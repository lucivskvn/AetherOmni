"""
Cloud Tasks Dispatcher — AetherOmni v2.0

In production (Cloud Run, DEBUG=False):
  - Dispatches OIDC-token-authenticated HTTP POST payloads to Google Cloud Tasks.
  - Cloud Tasks then POSTs to /internal/tasks/<task_name>/ on the same service.

In local development (DEBUG=True):
  - Skips metadata server lookups immediately (Gap E-39).
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
    tasks_v2 = None


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


# ── OIDC token helper ─────────────────────────────────────────────────────────


def _get_oidc_token(audience: str) -> str | None:
    """
    Fetch an OIDC token from the GCP metadata server for the given audience.
    Returns None immediately in DEBUG mode to avoid blocking local devs (Gap E-39).
    """
    if settings.DEBUG:
        return None
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request = google.auth.transport.requests.Request()
        return google.oauth2.id_token.fetch_id_token(request, audience)
    except Exception as exc:
        logger.warning("[CloudTasks] Could not fetch OIDC auth: %s", exc)
        return None


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

    if not _LOCAL_TASK_REGISTRY:
        from extractor.task_handlers import TASK_REGISTRY

        _LOCAL_TASK_REGISTRY.update(TASK_REGISTRY)

    def _run() -> None:
        try:
            handler = _LOCAL_TASK_REGISTRY.get(task_name)
            if handler is None:
                logger.error("[CloudTasks/local] Unknown task: '%s'", task_name)
                return
            handler(payload)
        except Exception:
            logger.exception("[CloudTasks/local] Task '%s' raised an exception", task_name)

    thread = threading.Thread(target=_run, daemon=True, name=f"ct-{task_name}")
    thread.start()


def get_gcp_service_account() -> str | None:
    """Fetch the service account email from the local GCP Metadata Server."""
    import urllib.request

    try:
        req = urllib.request.Request(
            "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email",
            headers={"Metadata-Flavor": "Google"},
        )
        with urllib.request.urlopen(req, timeout=1) as response:
            return response.read().decode("utf-8").strip()
    except Exception:
        return None


def _enqueue_cloud(task_name: str, payload: dict, countdown: int) -> None:
    """Create a Cloud Tasks HTTP target task for production delivery."""
    details = get_gcp_project_details()
    if not details or not details.get("project_id"):
        logger.info("[CloudTasks] Not running on GCP (project details missing). Falling back to local thread.")
        _enqueue_local(task_name, payload)
        return

    project = details.get("project_id")
    region = details.get("region") or getattr(settings, "GCP_REGION", "us-central1")
    queue_name = getattr(settings, "CLOUD_TASKS_QUEUE", "omnirag-tasks")
    service_url = getattr(settings, "WORKER_URL", "") or getattr(settings, "APP_URL", "")

    if not project or not service_url:
        logger.error(
            "[CloudTasks] GCP_PROJECT or APP_URL not configured. Cannot dispatch task '%s'. "
            "Falling back to local thread.",
            task_name,
        )
        _enqueue_local(task_name, payload)
        return

    handler_url = f"{service_url.rstrip('/')}/internal/tasks/{task_name}/"
    queue_path = f"projects/{project}/locations/{region}/queues/{queue_name}"
    body = json.dumps(payload).encode()

    service_account = get_gcp_service_account() or f"{project}@appspot.gserviceaccount.com"

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
    except Exception:
        logger.exception("[CloudTasks] Failed to enqueue task '%s', falling back to local thread", task_name)
        _enqueue_local(task_name, payload)
