"""
KORDA Cloud Tasks Dispatcher

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
import uuid
from typing import Any

from django.conf import settings

from extractor.task_state import CANCEL_REQUESTED, CLOUD_TASK_NAME

logger = logging.getLogger(__name__)
_LOG_FORMAT = "[CloudTasks] %s"

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
    # BUG-14: Validate document tasks have a usable identifier before dispatch.
    # Enqueueing with a null/empty UUID wastes Cloud Tasks quota and causes
    # workers to log "missing document_uuid" and silently return.
    from extractor.utils import REEMBED_DOCUMENT_TASK

    if task_name in ("process_document", REEMBED_DOCUMENT_TASK):
        doc_ref = payload.get("document_uuid") or payload.get("document_id")
        if not doc_ref:
            raise ValueError(
                f"enqueue('{task_name}') called with missing document_uuid/document_id. "
                "Payload must include a non-null document identifier."
            )

    safe_countdown = max(0, min(int(countdown or 0), 86400 * 30))
    if settings.DEBUG:
        _enqueue_local(task_name, payload)
    else:
        _enqueue_cloud(task_name, payload, safe_countdown)


# Test seam: tests inject handlers via patch.dict; production always falls
# through to the real registry when empty.
_LOCAL_TASK_REGISTRY: dict[str, Any] = {}



def _enqueue_local(task_name: str, payload: dict) -> None:
    """Execute task in a daemon thread (local development fallback)."""
    logger.info("[CloudTasks/local] Spawning thread for task '%s'", task_name)

    from extractor.utils import REEMBED_DOCUMENT_TASK

    if task_name in {"process_document", REEMBED_DOCUMENT_TASK}:
        payload = dict(payload)
        payload.setdefault(CLOUD_TASK_NAME, f"local-{uuid.uuid4().hex}")
        _persist_document_task(task_name, payload)

    from extractor.task_handlers import get_task_registry

    task_registry = get_task_registry()

    def _run() -> None:
        from extractor import surreal_db
        from extractor.task_state import CLOUD_TASK_NAME
        from extractor.utils import REEMBED_DOCUMENT_TASK

        task_identity = ""
        if task_name in {"process_document", REEMBED_DOCUMENT_TASK}:
            task_identity = payload.get(CLOUD_TASK_NAME, "")
        token = surreal_db.document_task_name.set(task_identity)
        try:
            handler = task_registry.get(task_name)
            if handler is None:
                logger.error("[CloudTasks/local] Unknown task: '%s'", task_name)
                return
            handler(payload)
        except Exception as task_err:
            logger.exception("[CloudTasks/local] Task '%s' raised an exception: %s", task_name, task_err)
        finally:
            surreal_db.document_task_name.reset(token)

    thread = threading.Thread(target=_run, daemon=True, name=f"ct-{task_name}")
    thread.start()


def _enqueue_cloud(task_name: str, payload: dict, countdown: int) -> None:
    """Create a Cloud Tasks HTTP target task for production delivery."""
    details = get_gcp_project_details()
    if not details or not details.get("project_id"):
        message = "Cloud Tasks project details are unavailable; refusing to run a production task on the web service."
        logger.error(_LOG_FORMAT, message)
        raise RuntimeError(message)

    project = details.get("project_id")
    region = (
        getattr(settings, "GCP_QUEUE_LOCATION", None)
        or details.get("region")
        or getattr(settings, "GCP_REGION", "asia-southeast1")
    )
    queue_name = getattr(settings, "CLOUD_TASKS_QUEUE", "extractor-tasks")
    service_url = getattr(settings, "WORKER_URL", "")

    if not project or not service_url:
        message = f"Cloud Tasks worker URL is not configured; task '{task_name}' was not dispatched."
        logger.error(_LOG_FORMAT, message)
        raise RuntimeError(message)

    service_account = (getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "") or "").strip()
    if not service_account:
        message = "Cloud Tasks OIDC service account is not configured; dispatch was not attempted."
        logger.error(_LOG_FORMAT, message)
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

    from extractor.utils import REEMBED_DOCUMENT_TASK

    task_resource_name = f"{queue_path}/tasks/{uuid.uuid4().hex}"
    if task_name in {"process_document", REEMBED_DOCUMENT_TASK}:
        payload = {**payload, CLOUD_TASK_NAME: task_resource_name}
    body = json.dumps(payload, cls=_CloudTasksEncoder).encode()

    task: dict[str, Any] = {
        "name": task_resource_name,
        "http_request": {
            "http_method": "POST",
            "url": handler_url,
            "headers": {"Content-Type": "application/json"},
            "body": body,
            "oidc_token": {
                "service_account_email": service_account,
                "audience": service_url.rstrip("/"),
            },
        },
    }
    if task_name in {"process_document", REEMBED_DOCUMENT_TASK}:
        # HTTP targets default to a 10-minute dispatch deadline, but pipeline
        # stages can legitimately run up to the 25-minute heartbeat ceiling.
        # Without an explicit deadline every long document is marked
        # DEADLINE_EXCEEDED and retried while the original worker is still
        # running, duplicating LLM spend. 1740s stays under the 1800s max.
        import datetime as _dt

        task["dispatch_deadline"] = _dt.timedelta(seconds=1740)
    if countdown > 0:
        import datetime

        from google.protobuf import timestamp_pb2

        schedule_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=countdown)
        ts = timestamp_pb2.Timestamp()
        ts.FromDatetime(schedule_time)
        task["schedule_time"] = ts

    # Document pipelines can legitimately run ~25 minutes (the worker heartbeat
    # budget is 1500s). Without an explicit dispatch deadline Cloud Tasks gives
    # up at its ~10-minute default and retries while the first delivery is
    # still running. 1740s covers the heartbeat budget with margin and stays
    # under the 30-minute HTTP-task ceiling; the worker request timeout
    # (service-worker.yaml / Pulumi) is aligned to 1800s.
    task["dispatch_deadline"] = {"seconds": 1740}

    try:
        _persist_document_task(task_name, payload)
        client = _get_tasks_client()
        response = client.create_task(parent=queue_path, task=task)
        logger.info("[CloudTasks] Task '%s' enqueued: %s", task_name, response.name)
    except Exception as exc:
        logger.exception("[CloudTasks] Failed to enqueue task '%s'", task_name)
        raise RuntimeError(f"Cloud Tasks could not enqueue '{task_name}'.") from exc


def document_task_still_queued(task_name: str | None) -> bool:
    """Best-effort check that a persisted Cloud Tasks name still exists.

    Used by the stale-task reaper to distinguish a PENDING document that is
    genuinely waiting in the queue from one whose delivery was lost. Returns
    False when the task is gone, the name is missing, or the lookup itself
    fails — an unverifiable task is conservatively treated as queued so a
    transient API error cannot trigger a duplicate replacement enqueue.
    """
    if not task_name or settings.DEBUG or getattr(settings, "SURREALDB_OFFLINE", False):
        return False
    try:
        from google.api_core.exceptions import NotFound

        _get_tasks_client().get_task(name=task_name)
        return True
    except NotFound:
        return False
    except Exception as exc:
        # An unverifiable task is treated as still queued: reaping on a
        # transient lookup error would replace a live delivery and burn retry
        # budget. The next reaper sweep re-checks once the API recovers.
        logger.warning("[CloudTasks] Could not verify queued task %s: %s", task_name, exc)
        return True


def _persist_document_task(task_name: str, payload: dict) -> None:
    """Persist the name before dispatch, so a fast worker or cancellation sees it."""
    from extractor.utils import REEMBED_DOCUMENT_TASK

    if task_name not in {"process_document", REEMBED_DOCUMENT_TASK}:
        return
    from extractor import surreal_db

    doc_uuid = payload.get("document_uuid") or payload.get("document_id")
    updated = surreal_db.update_document(str(doc_uuid), {CLOUD_TASK_NAME: payload[CLOUD_TASK_NAME]})
    if updated.get(CLOUD_TASK_NAME) != payload[CLOUD_TASK_NAME]:
        raise RuntimeError("Could not persist the task identity; dispatch was not attempted.")


_IN_FLIGHT_STATUSES = ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]


def cancel_document_task(doc_uuid: str, document: dict) -> None:
    """Persist cancellation before deleting queued work; retain identity on failure."""
    from google.api_core.exceptions import NotFound

    from extractor import surreal_db

    # Status-guarded write: if a worker reached a terminal state between the
    # caller's snapshot and this write, do not flap a COMPLETED document back
    # to FAILED. The queued-task cleanup below still runs so a stranded
    # Cloud Tasks entry cannot resurrect the document.
    updated = surreal_db.update_document_if_status(
        doc_uuid,
        {"status": "FAILED", CANCEL_REQUESTED: True, "error_message": "Processing stopped by user."},
        allowed_statuses=_IN_FLIGHT_STATUSES,
    )
    if updated is None:
        updated = surreal_db.get_document(doc_uuid) or document
        if not updated:
            raise RuntimeError("Cancellation could not be saved. Please retry.")
    task_name = updated.get(CLOUD_TASK_NAME) or document.get(CLOUD_TASK_NAME)
    if not task_name or settings.DEBUG:
        return
    try:
        _get_tasks_client().delete_task(name=task_name)
    except NotFound:
        # Already dispatched or deleted; cooperative worker checks remain necessary.
        return
    except Exception as exc:
        raise RuntimeError(
            "Processing is marked stopped, but queue cleanup failed. Please retry cancellation."
        ) from exc
