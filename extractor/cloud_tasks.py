"""
Cloud Tasks Dispatcher — AetherOmni v2.0

In production (Cloud Run, DEBUG=False):
  - Dispatches OIDC-token-authenticated HTTP POST payloads to Google Cloud Tasks.
  - Cloud Tasks then POSTs to /internal/tasks/<task_name>/ on the same service.

In local development (DEBUG=True):
  - Skips metadata server lookups immediately.
  - Falls back to executing task functions inside a background daemon thread
    so the developer experience is identical to production without requiring
    Cloud Tasks infrastructure.
"""

from __future__ import annotations

import json
import logging
import threading
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

from extractor.deployment import get_gcp_project_details

try:
    from google.cloud import tasks_v2
except ImportError:
    tasks_v2 = None  # type: ignore[assignment]


# ── Production: Google Cloud Tasks client (lazy import) ───────────────────────
# Only imported when DEBUG=False so local installs don't need the SDK.
_tasks_client = None
_tasks_client_lock = threading.Lock()


def _get_tasks_client():
    """Return a shared Cloud Tasks client, initialised once."""
    global _tasks_client
    if _tasks_client is None:
        with _tasks_client_lock:
            if _tasks_client is None:
                from google.cloud import tasks_v2

                _tasks_client = tasks_v2.CloudTasksClient()
    return _tasks_client


# ── Core dispatcher ───────────────────────────────────────────────────────────


def enqueue(task_name: str, payload: dict[str, Any], countdown: int = 0) -> None:
    """
    Dispatch a task by name.

    - In production: creates a Cloud Tasks HTTP target task.
    - In development: runs the task in a background daemon thread.

    Args:
        task_name: Name registered in task_handlers.TASK_REGISTRY
                   (e.g. "process_document", "cleanup_expired_documents").
        payload:   JSON-serialisable dict forwarded verbatim to the handler.
        countdown: Seconds to delay before delivery (production only).
    """
    if settings.DEBUG:
        _enqueue_local(task_name, payload)
    else:
        _enqueue_cloud(task_name, payload, countdown)


_LOCAL_TASK_REGISTRY: dict[str, Any] = {}


def _enqueue_local(task_name: str, payload: dict) -> None:
    """Execute task in a daemon thread (local development fallback)."""
    logger.info("[CloudTasks/local] Spawning thread for task '%s'", task_name)

    from extractor.task_handlers import get_task_registry

    task_registry = _LOCAL_TASK_REGISTRY if _LOCAL_TASK_REGISTRY else get_task_registry()

    def _run() -> None:
        try:
            handler = task_registry.get(task_name)
            if handler is None:
                logger.error("[CloudTasks/local] Unknown task: '%s'", task_name)
                return
            handler(payload)
        except Exception as task_err:
            logger.exception("[CloudTasks/local] Task '%s' raised an exception: %s", task_name, task_err)

    thread = threading.Thread(target=_run, daemon=True, name=f"ct-{task_name}")
    thread.start()


def get_gcp_service_account() -> str | None:
    """Fetch the service account email from the local GCP Metadata Server."""
    import urllib.request

    from extractor.utils import validate_url_scheme

    url = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email"  # nosemgrep
    try:
        validate_url_scheme(url)
        req = urllib.request.Request(  # nosemgrep
            url,
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as response:  # nosec B310 # nosemgrep
            return response.read().decode("utf-8").strip()
    except Exception:
        return None


def _enqueue_cloud(task_name: str, payload: dict, countdown: int) -> None:
    """Create a Cloud Tasks HTTP target task for production delivery."""
    details = get_gcp_project_details()
    if not details or not details.get("project_id"):
        message = "Cloud Tasks project details are unavailable; refusing to run a production task on the web service."
        logger.error("[CloudTasks] %s", message)
        raise RuntimeError(message)

    project = details.get("project_id")
    region = details.get("region") or getattr(settings, "GCP_REGION", "asia-southeast1")
    queue_name = getattr(settings, "CLOUD_TASKS_QUEUE", "extractor-tasks")
    service_url = getattr(settings, "WORKER_URL", "") or getattr(settings, "APP_URL", "")

    if not project or not service_url:
        message = f"Cloud Tasks worker URL is not configured; task '{task_name}' was not dispatched."
        logger.error("[CloudTasks] %s", message)
        raise RuntimeError(message)

    handler_url = f"{service_url.rstrip('/')}/internal/tasks/{task_name}/"
    queue_path = f"projects/{project}/locations/{region}/queues/{queue_name}"
    from django.core.serializers.json import DjangoJSONEncoder

    class _CloudTasksEncoder(DjangoJSONEncoder):
        def default(self, obj):
            try:
                return super().default(obj)
            except TypeError:
                return str(obj)

    body = json.dumps(payload, cls=_CloudTasksEncoder).encode()

    # Prefer the project-number-based compute SA (Cloud Run default SA format).
    # Fall back to the appspot SA if project_number is unavailable.
    project_number = details.get("project_number") or project
    default_sa = f"{project_number}-compute@developer.gserviceaccount.com"
    service_account = get_gcp_service_account() or default_sa

    task: dict[str, Any] = {
        "http_request": {
            "http_method": "POST",
            "url": handler_url,
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "oidc_token": {
                "service_account_email": service_account,
                "audience": handler_url,
            },
        }
    }
    if countdown > 0:
        import datetime

        from google.protobuf import timestamp_pb2

        schedule_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=countdown)
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(schedule_time)
        task["schedule_time"] = ts

    try:
        client = _get_tasks_client()
        response = client.create_task(parent=queue_path, task=task)
        logger.info("[CloudTasks] Task '%s' enqueued: %s", task_name, response.name)
    except Exception as exc:
        logger.exception("[CloudTasks] Failed to enqueue task '%s'", task_name)
        raise RuntimeError(f"Cloud Tasks could not enqueue '{task_name}'.") from exc
