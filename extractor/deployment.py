import json
import logging
import os
import re
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
    except (ImportError, KeyError, AttributeError):
        env["HOME"] = os.path.expanduser("~")
    return env


def _get_ann_value(annotations_dicts, keys):
    for ann in annotations_dicts:
        for key in keys:
            if key in ann:
                try:
                    return int(ann[key])
                except (ValueError, TypeError) as num_err:
                    logger.debug(
                        "[Deployment] Could not parse scaling annotation '%s' value '%s': %s", key, ann[key], num_err
                    )
    return None


def _collect_annotations(config: dict) -> list[dict]:
    """Helper to cleanly extract annotation dicts from spec and metadata without deep nesting."""
    ann_dicts = []
    spec_ann = config.get("spec", {}).get("template", {}).get("metadata", {}).get("annotations", {})
    if isinstance(spec_ann, dict) and spec_ann:
        ann_dicts.append(spec_ann)
    meta_ann = config.get("metadata", {}).get("annotations", {})
    if isinstance(meta_ann, dict) and meta_ann:
        ann_dicts.append(meta_ann)
    return ann_dicts


def extract_knative_scaling(config, default_min, default_max):
    """Safely extracts min and max scaling values from Knative configuration.

    Supports both Knative standards and GCP-native (run.googleapis.com) annotations
    at both the service metadata level and the revision template metadata level.
    """
    if not config or not isinstance(config, dict):
        return default_min, default_max

    annotations_dicts = _collect_annotations(config)

    min_keys = [KNATIVE_MIN_SCALE, "run.googleapis.com/minScale"]
    max_keys = ["autoscaling.knative.dev/maxScale", "run.googleapis.com/maxScale"]

    min_val = _get_ann_value(annotations_dicts, min_keys)
    max_val = _get_ann_value(annotations_dicts, max_keys)

    return (
        default_min if min_val is None else min_val,
        default_max if max_val is None else max_val,
    )


def _query_metadata_server(path):
    import urllib.request

    try:
        req = urllib.request.Request(
            f"http://metadata.google.internal/computeMetadata/v1/{path}",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            return response.read().decode("utf-8").strip()
    except Exception:
        return None


def _detect_gcloud_project_id() -> str | None:
    try:
        res = subprocess.run(
            ["gcloud", "config", "get-value", "project"],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )  # nosec B603 B607
        if res.returncode == 0 and res.stdout.strip():
            logger.info("[Deployment] Auto-detected local gcloud project ID: %s", res.stdout.strip())
            return res.stdout.strip()
    except (subprocess.SubprocessError, OSError, FileNotFoundError) as proc_err:
        logger.debug("[Deployment] gcloud CLI project lookup skipped: %s", proc_err)
    return None


def _fetch_gcp_region():
    region_full = _query_metadata_server("instance/region")
    if region_full:
        return region_full.split("/")[-1]
    return None


def _detect_production_gcp_project(project_id, region):
    project_number = None
    if not project_id:
        project_id = _query_metadata_server("project/project-id") or ""
        if not project_id:
            logger.debug("[Deployment] Metadata server unreachable (not on GCP)")

    if not os.getenv("GCP_REGION"):
        region = _fetch_gcp_region() or region

    project_number = _query_metadata_server("project/numeric-project-id")
    return project_id, region, project_number


def get_gcp_project_details():
    """Retrieves the GCP project ID and region from environment variables

    or the local GCP Metadata Server.
    Returns None for project_id if not configured (no hardcoded fallback).
    Skips metadata server in DEBUG mode to avoid blocking local developers.
    Also falls back to local gcloud CLI config for ease of local development.
    """
    from django.conf import settings as django_settings

    project_id = os.getenv("GCP_PROJECT_ID") or os.getenv("GCP_PROJECT", "")
    region = os.getenv("GCP_REGION", "asia-southeast1")
    project_number = None

    # Fallback to local gcloud config if not set in environment
    if not project_id:
        project_id = _detect_gcloud_project_id() or ""

    if not django_settings.DEBUG:
        project_id, region, project_number = _detect_production_gcp_project(project_id, region)

    return {
        "project_id": project_id or None,
        "project_number": project_number or project_id or None,
        "region": region,
        # Service names are configurable via environment variables so renaming
        # Cloud Run services (e.g. data-extractor-web -> aether-web) requires
        # only a YAML env var change, not a code change.
        "web_service": os.getenv("GCP_WEB_SERVICE") or getattr(django_settings, "GCP_WEB_SERVICE", "aether-web"),
        "worker_service": os.getenv("GCP_WORKER_SERVICE")
        or getattr(django_settings, "GCP_WORKER_SERVICE", "aether-worker"),
    }


def get_gcp_access_token():
    """
    Fetches an OAuth2 access token from the local GCP Metadata Server.
    Returns None immediately in DEBUG mode to avoid blocking local devs.
    """
    from django.conf import settings as django_settings

    if django_settings.DEBUG:
        logger.debug("[Deployment] Skipping metadata credential fetch in DEBUG mode.")
        return None

    try:
        req = urllib.request.Request(  # nosemgrep: python.lang.security.audit.insecure-transport.urllib.insecure-request-object.insecure-request-object
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            data = json.loads(response.read().decode("utf-8"))
            return data["access_token"]
    except Exception as exc:
        logger.debug("[Deployment] Metadata server unreachable: %s", exc)
        return None


def validate_service_name(service_name):
    if not service_name or not re.match(r"^[a-z]([-a-z0-9]*[a-z0-9])?$", str(service_name)):
        raise ValueError(f"Invalid service name: {service_name}")

def validate_region(region):
    if not region or not re.match(r"^[a-z]+-[a-z]+\d+$", str(region)):
        raise ValueError(f"Invalid region: {region}")

def validate_project_namespace(project_namespace):
    if not project_namespace or not re.match(r"^[a-z0-9-]+$", str(project_namespace)):
        raise ValueError(f"Invalid project namespace: {project_namespace}")

def get_service_config(service_name):
    """
    Fetches the active Knative service JSON configuration.
    Uses Google REST API in production or falls back to 'gcloud' CLI locally.
    """
    validate_service_name(service_name)
    details = get_gcp_project_details()
    project_id = details["project_id"]
    project_namespace = details.get("project_number") or project_id
    region = details["region"]

    if not project_id:
        raise ValueError("GCP Project ID is not configured.")

    validate_region(region)
    validate_project_namespace(project_namespace)

    token = get_gcp_access_token()
    if token:
        # GCP REST API (Knative v1)
        url = f"https://{region}-run.googleapis.com/apis/serving.knative.dev/v1/namespaces/{project_namespace}/services/{service_name}"
        try:
            req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": APPLICATION_JSON})
            with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as he:
            body = he.read().decode("utf-8") if he.fp else ""
            logger.exception("GCP REST API describe HTTPError %d: %s for %s", he.code, body, service_name)
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


def _clean_read_only_metadata(service_json):
    if "metadata" in service_json:
        metadata = service_json["metadata"]
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


def _ensure_annotations_spec(service_json):
    try:
        return service_json["spec"]["template"]["metadata"]["annotations"]
    except KeyError:
        if "template" not in service_json["spec"]:
            service_json["spec"]["template"] = {}
        if "metadata" not in service_json["spec"]["template"]:
            service_json["spec"]["template"]["metadata"] = {}
        service_json["spec"]["template"]["metadata"]["annotations"] = {}
        return service_json["spec"]["template"]["metadata"]["annotations"]


def update_service_scale(service_name, min_scale, max_scale):
    """
    Updates the scaling settings of a Cloud Run service (min and max scale).
    Uses GCP REST PUT API in production or falls back to local gcloud updates.
    """
    validate_service_name(service_name)
    details = get_gcp_project_details()
    project_id = details["project_id"]
    project_namespace = details.get("project_number") or project_id
    region = details["region"]

    validate_region(region)
    validate_project_namespace(project_namespace)

    # 1. Fetch current service config first (required for Knative PUT updates)
    service_json = get_service_config(service_name)

    # Clean read-only status and metadata fields GCP rejects on PUT
    _clean_read_only_metadata(service_json)

    # Inject annotations in spec template
    annotations = _ensure_annotations_spec(service_json)

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
            logger.exception("GCP REST API update HTTPError %d: %s for %s", he.code, body, service_name)
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


def _parse_http_request_payload(entry):
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
        return f"HTTP {method} {path} - Status: {status} - Latency: {latency}"
    return None


def _parse_text_payload(entry):
    return (
        entry.get("textPayload")
        or entry.get("jsonPayload", {}).get("message")
        or entry.get("protoPayload", {}).get("resourceName", "")
        or ""
    )


def _fallback_local_run_logs(service_name, region, project_id, limit):
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
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 # nosemgrep: python.lang.security.audit.dynamic-urllib-use-detected.dynamic-urllib-use-detected
            response_data = json.loads(response.read().decode("utf-8"))
            entries = response_data.get("entries", [])
            logs_parsed = []
            for entry in entries:
                timestamp = entry.get("timestamp", "")
                severity = entry.get("severity", "INFO")
                payload = _parse_http_request_payload(entry) or _parse_text_payload(entry)
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
            payload = _parse_http_request_payload(entry) or _parse_text_payload(entry)
            if payload:
                logs_parsed.append({"timestamp": timestamp, "message": payload, "severity": severity})
        return logs_parsed

    except Exception:
        return _fallback_local_run_logs(service_name, region, project_id, limit)


def get_service_logs(service_name, limit=50):
    """
    Fetches the latest live logs from Google Cloud Logging API (production)
    or falls back to 'gcloud run services logs' (local).
    """
    validate_service_name(service_name)
    details = get_gcp_project_details()
    project_id = details["project_id"]
    region = details["region"]

    if not project_id:
        return [{"timestamp": "", "message": "GCP Project ID not configured.", "severity": "WARNING"}]

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
        logger.debug("[Audits] Ruff formatter binary not found during automated check pass.")

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
