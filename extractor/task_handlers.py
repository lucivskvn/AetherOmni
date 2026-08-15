"""
Cloud Tasks Webhook Receivers — AetherOmni v2.0

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
    "35.199.0.0/16",  # NOSONAR python:S1313 -- Google Cloud Tasks documented egress CIDR range
    "34.64.0.0/10",  # NOSONAR python:S1313 -- Google Cloud asia-southeast1 region IP pool
    "34.128.0.0/10",  # NOSONAR python:S1313 -- Google Cloud additional compute IP pool
    "107.178.0.0/16",  # NOSONAR python:S1313 -- Google Cloud Tasks egress observed in asia-southeast1 production
    "34.2.0.0/16",  # NOSONAR python:S1313 -- Google Cloud additional egress IP pool
    "130.211.0.0/22",  # NOSONAR python:S1313 -- Google Cloud Load Balancers and internal egress
    "35.191.0.0/16",  # NOSONAR python:S1313 -- Google Cloud health check probes and internal CIDR
]

from collections.abc import Callable

# ── Task function registry ────────────────────────────────────────────────────
# Maps task_name strings (URL slug) to callable handler functions.
# Populated at import time.
TASK_REGISTRY: dict[str, Callable] = {}


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


def _verify_oidc_token(request: HttpRequest, audience: str | list[str]) -> bool:
    """
    Verify the Google OIDC Bearer token in the Authorization header.
    Returns True if valid against any accepted audience URL, False otherwise.
    Always returns True in DEBUG mode.
    """
    if settings.DEBUG:
        return True

    auth_header = request.META.get("HTTP_AUTHORIZATION", "")
    if not auth_header.startswith("Bearer "):
        logger.warning("[CloudTasksHandler] Missing or malformed auth header.")
        return False

    token = auth_header.split(" ", 1)[1]
    audiences = [audience] if isinstance(audience, str) else list(audience)

    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request_obj = google.auth.transport.requests.Request()
        for aud in audiences:
            if not aud:
                continue
            try:
                id_info = google.oauth2.id_token.verify_oauth2_token(token, request_obj, aud)
                if id_info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
                    logger.warning("[CloudTasksHandler] OIDC issuer unexpected: %s", id_info.get("iss"))
                    return False

                expected_service_account = getattr(settings, "CLOUD_TASKS_SERVICE_ACCOUNT", "").strip()
                if expected_service_account:
                    if id_info.get("email") != expected_service_account or not id_info.get("email_verified"):
                        logger.warning(
                            "[CloudTasksHandler] OIDC caller is not the configured Cloud Tasks service account."
                        )
                        return False
                elif not settings.DEBUG:
                    logger.warning("[CloudTasksHandler] CLOUD_TASKS_SERVICE_ACCOUNT must be configured in production.")
                    return False

                return True
            except Exception as aud_err:
                logger.debug("[CloudTasksHandler] Candidate audience '%s' rejected: %s", aud, aud_err)

        logger.warning("[CloudTasksHandler] OIDC verification failed for all candidate audiences.")
        return False
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

    raw_remote = request.META.get("REMOTE_ADDR", "").strip()
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate_ips: list[str] = []
    if raw_remote:
        candidate_ips.append(raw_remote)
    if x_forwarded_for:
        # Check all verifiable proxy hops rather than trusting raw leftmost header
        candidate_ips.extend([ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()])

    for ip_str in candidate_ips:
        try:
            ip = ipaddress.ip_address(ip_str)
            for cidr in GOOGLE_TASKS_IP_CIDRS:
                if ip in ipaddress.ip_network(cidr, strict=False):
                    return True
        except ValueError:
            continue

    logger.warning("[CloudTasksHandler] Request from unrecognised IP: %s (remote: %s)", x_forwarded_for, raw_remote)
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
        worker_url = getattr(settings, "WORKER_URL", "").rstrip("/")
        app_url = getattr(settings, "APP_URL", "http://localhost:8080").rstrip("/")

        audiences = [f"{(worker_url or app_url)}/internal/tasks/{task_name}/"]
        if worker_url:
            audiences.append(f"{worker_url}/internal/tasks/{task_name}/")
        if app_url:
            audiences.append(f"{app_url}/internal/tasks/{task_name}/")

        if not _verify_oidc_token(request, audiences):
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
            return JsonResponse(
                {"status": "error", "task": task_name, "detail": "Task execution failed; see Cloud Run logs."}
            )

        return JsonResponse({"status": "ok", "task": task_name})
