import json
import logging
import os
import subprocess  # nosec B404
import sys
import urllib.request

from extractor.utils import APPLICATION_JSON, KNATIVE_MIN_SCALE

logger = logging.getLogger(__name__)


def _get_subprocess_env():
    env = os.environ.copy()
    try:
        import pwd

        # Restore true user home directory to allow gcloud fallback to find user credentials
        env["HOME"] = pwd.getpwuid(os.getuid()).pw_dir
    except ImportError:
        pass
    return env


def extract_knative_scaling(config, default_min, default_max):
    """
    Safely extracts min and max scaling values from Knative configuration.
    Supports both Knative standards and GCP-native (run.googleapis.com) annotations
    at both the service metadata level and the revision template metadata level.
    """
    if not config:
        return default_min, default_max

    # Gather all possible annotations dictionaries
    annotations_dicts = []
    try:
        ann_template = config.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
        if ann_template:
            annotations_dicts.append(ann_template)
    except AttributeError:
        pass
    try:
        ann_meta = config.get("metadata", {}).get("annotations", {})
        if ann_meta:
            annotations_dicts.append(ann_meta)
    except AttributeError:
        pass

    min_keys = [KNATIVE_MIN_SCALE, "run.googleapis.com/minScale"]
    max_keys = ["autoscaling.knative.dev/maxScale", "run.googleapis.com/maxScale"]

    min_val = None
    for ann in annotations_dicts:
        for key in min_keys:
            if key in ann:
                try:
                    min_val = int(ann[key])
                    break
                except (ValueError, TypeError):
                    pass
        if min_val is not None:
            break

    if min_val is None:
        min_val = default_min

    max_val = None
    for ann in annotations_dicts:
        for key in max_keys:
            if key in ann:
                try:
                    max_val = int(ann[key])
                    break
                except (ValueError, TypeError):
                    pass
        if max_val is not None:
            break

    if max_val is None:
        max_val = default_max

    return min_val, max_val


def get_gcp_project_details():
    """
    Retrieves the GCP project ID and region from environment variables
    or the local GCP Metadata Server.
    Gap E-25: returns None for project_id if not configured (no hardcoded fallback).
    Gap E-39: skips metadata server in DEBUG mode to avoid blocking local developers.
    """
    from django.conf import settings as django_settings

    project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("GCP_PROJECT", "")
    region = os.getenv("GCP_REGION", "asia-southeast1")
    project_number = None

    if not django_settings.DEBUG:
        if not project_id:
            try:
                req = urllib.request.Request(  # nosemgrep
                    "http://metadata.google.internal/computeMetadata/v1/project/project-id",
                    headers={"Metadata-Flavor": "Google"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 nosemgrep
                    project_id = response.read().decode("utf-8").strip()
            except Exception:
                logger.debug("[Deployment] Metadata server unreachable (not on GCP)")

        if not os.getenv("GCP_REGION"):
            try:
                req = urllib.request.Request(  # nosemgrep
                    "http://metadata.google.internal/computeMetadata/v1/instance/region",
                    headers={"Metadata-Flavor": "Google"},
                )
                with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 nosemgrep
                    region_full = response.read().decode("utf-8").strip()
                    region = region_full.split("/")[-1]
            except Exception:  # nosec B110
                pass

        project_number = None
        try:
            req = urllib.request.Request(  # nosemgrep
                "http://metadata.google.internal/computeMetadata/v1/project/numeric-project-id",
                headers={"Metadata-Flavor": "Google"},
            )
            with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 nosemgrep
                project_number = response.read().decode("utf-8").strip()
        except Exception as exc:
            logger.debug("[Deployment] Metadata project number unreachable: %s", exc)

    return {
        "project_id": project_id or None,
        "project_number": project_number or project_id or None,
        "region": region,
        "web_service": "data-extractor-web",
        "worker_service": "data-extractor-worker",
    }


def get_gcp_access_token():
    """
    Fetches an OAuth2 access token from the local GCP Metadata Server.
    Gap E-39: returns None immediately in DEBUG mode to avoid blocking local devs.
    """
    from django.conf import settings as django_settings

    if django_settings.DEBUG:
        logger.debug("[Deployment] Skipping metadata credential fetch in DEBUG mode (E-39).")
        return None

    try:
        req = urllib.request.Request(  # nosemgrep
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 nosemgrep
            data = json.loads(response.read().decode("utf-8"))
            return data["access_token"]
    except Exception as exc:
        logger.debug("[Deployment] Metadata server unreachable: %s", exc)
        return None


def get_service_config(service_name):
    """
    Fetches the active Knative service JSON configuration.
    Uses Google REST API in production or falls back to 'gcloud' CLI locally.
    """
    details = get_gcp_project_details()
    project_id = details["project_id"]
    project_namespace = details.get("project_number") or project_id
    region = details["region"]

    token = get_gcp_access_token()
    if token:
        # GCP REST API (Knative v1)
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_namespace}/services/{service_name}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": APPLICATION_JSON})
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8") if he.fp else ""
            logger.error("GCP REST API describe HTTPError %d: %s for %s", he.code, body, service_name)
            raise he
        except Exception as e:
            logger.exception(f"GCP REST API describe failed for {service_name}.")
            raise e
    else:
        # Local development fallback using gcloud CLI
        try:
            cmd = [
                "gcloud",
                "run",
                "services",
                "describe",
                service_name,
                "--region",
                region,
                "--project",
                project_id,
                "--format",
                "json",
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, env=_get_subprocess_env(), timeout=30)  # nosec B603
            return json.loads(output.decode("utf-8"))
        except Exception as e:
            logger.warning(f"Local gcloud describe failed for {service_name}: {e}")
            raise e


def update_service_scale(service_name, min_scale, max_scale):
    """
    Updates the scaling settings of a Cloud Run service (min and max scale).
    Uses GCP REST PUT API in production or falls back to local gcloud updates.
    """
    details = get_gcp_project_details()
    project_id = details["project_id"]
    project_namespace = details.get("project_number") or project_id
    region = details["region"]

    # 1. Fetch current service config first (required for Knative PUT updates)
    service_json = get_service_config(service_name)

    # Clean read-only status and metadata fields GCP rejects on PUT
    if "metadata" in service_json:
        metadata = service_json["metadata"]
        # Remove metadata values that are server-managed
        metadata.pop("generation", None)
        metadata.pop("resourceVersion", None)
        metadata.pop("selfLink", None)
        metadata.pop("uid", None)
        metadata.pop("creationTimestamp", None)
        if "annotations" in metadata:
            annotations = metadata["annotations"]
            annotations.pop("run.googleapis.com/urls", None)
            annotations.pop("run.googleapis.com/operation-id", None)
            annotations.pop("run.googleapis.com/ingress-status", None)
            annotations.pop("serving.knative.dev/creator", None)
            annotations.pop("serving.knative.dev/lastModifier", None)

    if "status" in service_json:
        service_json.pop("status")

    # Inject annotations in spec template
    try:
        annotations = service_json["spec"]["template"]["metadata"]["annotations"]
    except KeyError:
        if "template" not in service_json["spec"]:
            service_json["spec"]["template"] = {}
        if "metadata" not in service_json["spec"]["template"]:
            service_json["spec"]["template"]["metadata"] = {}
        service_json["spec"]["template"]["metadata"]["annotations"] = {}
        annotations = service_json["spec"]["template"]["metadata"]["annotations"]

    annotations[KNATIVE_MIN_SCALE] = str(min_scale)
    annotations["autoscaling.knative.dev/maxScale"] = str(max_scale)
    annotations["run.googleapis.com/minScale"] = str(min_scale)
    annotations["run.googleapis.com/maxScale"] = str(max_scale)

    token = get_gcp_access_token()
    if token:
        # GCP REST API (Knative PUT update)
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_namespace}/services/{service_name}"
        try:
            data = json.dumps(service_json).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": APPLICATION_JSON,
                    "Accept": APPLICATION_JSON,
                },
                method="PUT",
            )
            with urllib.request.urlopen(req, timeout=10) as response:  # nosec B310 nosemgrep
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8") if he.fp else ""
            logger.error("GCP REST API update HTTPError %d: %s for %s", he.code, body, service_name)
            raise he
        except Exception as e:
            logger.exception(f"GCP REST API update failed for {service_name}.")
            raise e
    else:
        # Local development fallback using gcloud CLI
        try:
            cmd = [
                "gcloud",
                "run",
                "services",
                "update",
                service_name,
                "--min-instances",
                str(min_scale),
                "--max-instances",
                str(max_scale),
                "--region",
                region,
                "--project",
                project_id,
                "--async",
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, env=_get_subprocess_env(), timeout=30)  # nosec B603
            return {"status": "success", "output": output.decode("utf-8")}
        except Exception as e:
            logger.exception(f"Local gcloud update failed for {service_name}.")
            raise e


def _get_service_logs_gcp(service_name, project_id, limit, token):
    url = "https://logging.googleapis.com/v2/entries:list"
    body = {
        "resourceNames": [f"projects/{project_id}"],
        "filter": f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service_name}"',
        "orderBy": "timestamp desc",
        "pageSize": limit,
    }
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Content-Type": APPLICATION_JSON},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
            response_data = json.loads(response.read().decode("utf-8"))
            entries = response_data.get("entries", [])
            logs_parsed = []
            for entry in entries:
                timestamp = entry.get("timestamp", "")
                severity = entry.get("severity", "INFO")
                http_req = entry.get("httpRequest")
                if http_req and isinstance(http_req, dict):
                    method = http_req.get("requestMethod", "GET")
                    url = http_req.get("requestUrl", "")
                    status = http_req.get("status", "")
                    latency = http_req.get("latency", "")
                    from urllib.parse import urlparse

                    try:
                        parsed = urlparse(url)
                        path = parsed.path
                        if parsed.query:
                            path += f"?{parsed.query}"
                    except Exception:
                        path = url
                    payload = f"HTTP {method} {path} - Status: {status} - Latency: {latency}"
                else:
                    payload = (
                        entry.get("textPayload")
                        or entry.get("jsonPayload", {}).get("message")
                        or entry.get("protoPayload", {}).get("resourceName", "")
                    )
                if payload:
                    logs_parsed.append({"timestamp": timestamp, "message": payload, "severity": severity})
            return logs_parsed
    except Exception as e:
        logger.exception("Failed to fetch logs via Logging REST API.")
        return [
            {
                "timestamp": "",
                "message": f"Could not fetch logs via REST API: {e!s}. Check service account permissions.",
                "severity": "ERROR",
            }
        ]


def _get_service_logs_local(service_name, project_id, region, limit):
    try:
        cmd = [
            "gcloud",
            "logging",
            "read",
            f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service_name}"',
            "--limit",
            str(limit),
            "--project",
            project_id,
            "--format",
            "json",
        ]
        output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, env=_get_subprocess_env(), timeout=30)  # nosec B603
        entries = json.loads(output.decode("utf-8"))
        logs_parsed = []
        for entry in entries:
            timestamp = entry.get("timestamp", "")
            severity = entry.get("severity", "INFO")
            http_req = entry.get("httpRequest")
            if http_req and isinstance(http_req, dict):
                method = http_req.get("requestMethod", "GET")
                url = http_req.get("requestUrl", "")
                status = http_req.get("status", "")
                latency = http_req.get("latency", "")
                from urllib.parse import urlparse

                try:
                    parsed = urlparse(url)
                    path = parsed.path
                    if parsed.query:
                        path += f"?{parsed.query}"
                except Exception:
                    path = url
                payload = f"HTTP {method} {path} - Status: {status} - Latency: {latency}"
            else:
                payload = entry.get("textPayload") or entry.get("jsonPayload", {}).get("message") or ""
            if payload:
                logs_parsed.append({"timestamp": timestamp, "message": payload, "severity": severity})
        return logs_parsed

    except Exception:
        try:
            cmd = [
                "gcloud",
                "run",
                "services",
                "logs",
                "read",
                service_name,
                "--region",
                region,
                "--project",
                project_id,
                "--limit",
                str(limit),
            ]
            output = subprocess.check_output(cmd, stderr=subprocess.STDOUT, env=_get_subprocess_env(), timeout=30)  # nosec B603
            lines = output.decode("utf-8").split("\n")
            logs_parsed = []
            for line in lines:
                if line.strip():
                    logs_parsed.append({"timestamp": "", "message": line, "severity": "INFO"})
            return logs_parsed
        except Exception as ex:
            return [{"timestamp": "", "message": f"Local log retrieval failed: {ex!s}", "severity": "ERROR"}]


def get_service_logs(service_name, limit=50):
    """
    Fetches the latest live logs from Google Cloud Logging API (production)
    or falls back to 'gcloud run services logs' (local).
    """
    details = get_gcp_project_details()
    project_id = details["project_id"]
    region = details["region"]

    token = get_gcp_access_token()
    if token:
        return _get_service_logs_gcp(service_name, project_id, limit, token)
    else:
        return _get_service_logs_local(service_name, project_id, region, limit)


def run_qa_checks():
    """
    Executes Ruff linting, formatting checks, and Django systems check
    within the workspace environment.
    """
    # Find base directory
    base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    results = []

    # 1. Ruff check
    try:
        # Legitimate QA subprocess invocation (untrusted input is not accepted)
        subprocess.check_output(["ruff", "check", "."], cwd=base_dir, stderr=subprocess.STDOUT, timeout=30)  # nosec B603 B607
        results.append("✓ [Ruff Linter] Passed. No issues found.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"✗ [Ruff Linter] Found Issues:\n{e.output.decode('utf-8')}\n")
    except FileNotFoundError:
        results.append("⚠️ [Ruff Linter] Ruff not installed in host/container.\n")

    # 2. Ruff format check
    try:
        # Legitimate QA subprocess invocation (untrusted input is not accepted)
        subprocess.check_output(["ruff", "format", "--check", "."], cwd=base_dir, stderr=subprocess.STDOUT, timeout=30)  # nosec B603 B607
        results.append("✓ [Ruff Formatter] Passed. Code formatting is consistent.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"✗ [Ruff Formatter] Code has formatting inconsistencies:\n{e.output.decode('utf-8')}\n")
    except FileNotFoundError:
        pass

    # 3. Django system check
    try:
        # Legitimate Django CLI invocation (untrusted input is not accepted)
        subprocess.check_output(
            [sys.executable, "manage.py", "check"], cwd=base_dir, stderr=subprocess.STDOUT, timeout=30
        )  # nosec B603
        results.append("✓ [Django System Check] Passed successfully.\n")
    except subprocess.CalledProcessError as e:
        results.append(f"✗ [Django System Check] Failed:\n{e.output.decode('utf-8')}\n")

    return "".join(results)
