"""
Cloud Tasks Webhook Receivers — OmniRAG Extractor v2.0

Handles authenticated HTTP POST callbacks from Google Cloud Tasks.
Each task type is registered in TASK_REGISTRY and dispatched to the
corresponding task function.

Security:
  - Production: verifies Google OIDC Bearer token on every request.
  - Production: validates request source against Google Cloud Tasks IP CIDRs.
  - CSRF exempt: Cloud Tasks uses Bearer auth, not session cookies.
  - Local dev: skips auth verification when DEBUG=True.
"""

from __future__ import annotations

import ipaddress
import json
import logging

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

logger = logging.getLogger(__name__)

# Google Cloud Tasks outbound IP CIDR ranges (as published by Google)
# https://cloud.google.com/vpc/docs/verify-google-ip-ranges
# NOTE: Google Cloud Tasks uses dynamic IPs from the broader Google IP pool.
# The OIDC token check is the primary security control; this IP check is
# defense-in-depth and should NOT block tasks that pass OIDC verification.
# To minimise false positives, we accept all published Google IP ranges.
GOOGLE_TASKS_IP_CIDRS = [
    "35.199.0.0/16",    # Cloud Tasks documented range
    "34.64.0.0/10",     # Google Cloud asia-southeast1
    "34.128.0.0/10",    # Google Cloud additional
    "107.178.0.0/16",   # Google Cloud Tasks egress (observed in asia-southeast1 production)
    "34.2.0.0/16",      # Google Cloud additional egress
    "130.211.0.0/22",   # Google Cloud Load Balancers / internal
    "35.191.0.0/16",    # Google health checks / internal
]

# ── Task function registry ────────────────────────────────────────────────────
# Maps task_name strings (URL slug) to callable handler functions.
# Populated at import time.
TASK_REGISTRY: dict[str, callable] = {}


def _register():
    """Populate the task registry after apps are fully loaded."""
    from extractor import tasks

    TASK_REGISTRY.update(
        {
            "process_document": tasks.process_document_task,
            "reembed_document": tasks.reembed_edited_document_task,
            "cleanup_expired_documents": tasks.cleanup_expired_documents_task,
            "reap_stale_tasks": tasks.reap_stale_tasks,
            "store_user_memory": tasks.store_user_memory_task,
        }
    )


# ── OIDC Bearer token verification ───────────────────────────────────────────


def _verify_oidc_token(request: HttpRequest, audience: str) -> bool:
    """
    Verify the Google OIDC Bearer token in the Authorization header.
    Returns True if valid, False otherwise.
    Always returns True in DEBUG mode.
    """
    if settings.DEBUG:
        return True

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        logger.warning("[CloudTasksHandler] Missing or malformed Authorization header.")
        return False

    token = auth_header.split(" ", 1)[1]
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request_obj = google.auth.transport.requests.Request()
        id_info = google.oauth2.id_token.verify_oauth2_token(token, request_obj, audience)
        if id_info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
            logger.warning("[CloudTasksHandler] OIDC issuer unexpected: %s", id_info.get("iss"))
            return False
        return True
    except Exception as exc:
        logger.warning("[CloudTasksHandler] OIDC verification failed: %s", exc)
        return False


def _verify_source_ip(request: HttpRequest) -> bool:
    """
    Verify the request source IP is within known Google Cloud Tasks CIDRs.
    Always returns True in DEBUG mode.
    """
    if settings.DEBUG:
        return True

    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if x_forwarded_for:
        raw_ip = x_forwarded_for.split(",")[0].strip()
    else:
        raw_ip = request.META.get("REMOTE_ADDR", "")

    try:
        client_ip = ipaddress.ip_address(raw_ip)
        for cidr in GOOGLE_TASKS_IP_CIDRS:
            if client_ip in ipaddress.ip_network(cidr, strict=False):
                return True
    except ValueError:
        pass

    logger.warning("[CloudTasksHandler] Request from unrecognised IP: %s", raw_ip)
    return False


# ── Main handler view ─────────────────────────────────────────────────────────


@method_decorator(csrf_exempt, name="dispatch")
class CloudTaskHandlerView(View):
    """
    Receives authenticated Cloud Tasks webhook callbacks and dispatches them
    to the appropriate task function.

    URL: /internal/tasks/<task_name>/
    """

    http_method_names = ["post"]

    def post(self, request: HttpRequest, task_name: str) -> HttpResponse:
        # Ensure registry is populated on first call
        if not TASK_REGISTRY:
            _register()

        # ── Security checks (production only) ─────────────────────────────
        app_url = getattr(settings, "APP_URL", "http://localhost:8080").rstrip("/")
        audience = f"{app_url}/internal/tasks/{task_name}/"

        if not _verify_oidc_token(request, audience):
            return HttpResponse("Unauthorized", status=401)

        if not _verify_source_ip(request):
            # WARNING: Do NOT return 4xx/5xx here — Cloud Tasks retries on non-2xx,
            # causing a retry storm. The OIDC token check above is the real security
            # gate. Log the unrecognised IP for monitoring and proceed.
            logger.warning(
                "[CloudTasksHandler] Proceeding despite unrecognised IP: %s — OIDC token already verified.",
                request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", "")),
            )

        # ── Dispatch ──────────────────────────────────────────────────────
        handler = TASK_REGISTRY.get(task_name)
        if handler is None:
            logger.error("[CloudTasksHandler] Unknown task name: '%s'", task_name)
            return JsonResponse({"error": f"Unknown task: {task_name}"}, status=404)

        try:
            body = request.body
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        logger.info("[CloudTasksHandler] Dispatching task '%s' with payload keys: %s", task_name, list(payload.keys()))

        try:
            handler(payload)
        except Exception:
            logger.exception("[CloudTasksHandler] Task '%s' raised an unhandled exception", task_name)
            # Return 200 OK even on internal pipeline failures — the pipeline
            # already marks the document as FAILED via _fail_document().
            # Returning 500 causes Cloud Tasks to retry infinitely, creating
            # duplicate executions and a retry storm.
            return JsonResponse({"status": "error", "task": task_name, "detail": "Task execution failed; see Cloud Run logs."})

        return JsonResponse({"status": "ok", "task": task_name})
