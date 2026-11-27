"""
KORDA Cloud Tasks Webhook Receivers

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
import re
from datetime import UTC

from django.conf import settings
from django.http import HttpRequest, HttpResponse, JsonResponse
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.csrf import csrf_exempt

from extractor.task_state import CANCEL_REQUESTED, CLOUD_TASK_NAME

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

import threading
from collections.abc import Callable

_TASK_REGISTRY: dict[str, Callable] = {}
TASK_REGISTRY = _TASK_REGISTRY
_REGISTRY_LOCK = threading.Lock()


def _register():
    """Populate the task registry after apps are fully loaded."""
    if not _TASK_REGISTRY:
        with _REGISTRY_LOCK:
            if not _TASK_REGISTRY:
                from extractor import tasks

                _TASK_REGISTRY.update(
                    {
                        "process_document": tasks.process_document_task,
                        "reembed_document": tasks.reembed_edited_document_task,
                        "cleanup_expired_documents": tasks.cleanup_expired_documents_task,
                        "reap_stale_tasks": tasks.reap_stale_tasks,
                        "store_user_memory": tasks.store_user_memory_task,
                        "digitalize_gcs_object": digitalize_gcs_object_task,
                    }
                )


def get_task_registry() -> dict[str, Callable]:
    """Returns the populated task registry, loading task handlers if not yet registered."""
    _register()
    return _TASK_REGISTRY


def _document_task_identity(task_name: str, payload: dict) -> str:
    """Return a write-fence identity only for a task that owns one document."""
    from extractor.utils import REEMBED_DOCUMENT_TASK

    if task_name in {"process_document", REEMBED_DOCUMENT_TASK}:
        return payload.get(CLOUD_TASK_NAME, "")
    return ""


def _claim_timestamp(doc: dict):
    """Return the document's claim timestamp as an aware datetime, or None."""
    updated_at = doc.get("updated_at")
    if isinstance(updated_at, str):
        updated_at = parse_datetime(updated_at)
    if updated_at is None:
        return None
    if timezone.is_naive(updated_at):
        updated_at = timezone.make_aware(updated_at, UTC)
    return updated_at


def _has_active_document_claim(doc: dict, claimed_before=None) -> bool:
    """Return whether a non-terminal document claim has been refreshed recently.

    When ``claimed_before`` is given, a claim refreshed *after* that instant
    belongs to the current delivery's own writes and does not count as an
    independent active worker (AUD-282).
    """
    updated_at = _claim_timestamp(doc)
    if updated_at is None:
        # A non-terminal document with no freshness signal is not an active
        # claim — treat it as stale so the delivery stays retryable and the
        # reaper can recover it.
        return False
    stale_cutoff = timezone.now() - timezone.timedelta(minutes=5)
    return not (updated_at <= stale_cutoff or (claimed_before is not None and updated_at > claimed_before))


def _confirm_document_outcome(task_name: str, payload: dict, delivery_started=None) -> None:
    """Do not acknowledge an unfinished delivery after a worker/storage failure."""
    from extractor.utils import REEMBED_DOCUMENT_TASK

    if task_name not in {"process_document", REEMBED_DOCUMENT_TASK}:
        return
    from extractor import surreal_db

    doc_uuid = payload.get("document_uuid") or payload.get("document_id")
    doc = surreal_db.get_document(doc_uuid) if doc_uuid else None
    if not doc or doc.get("status") in {"COMPLETED", "FAILED"} or doc.get(CANCEL_REQUESTED):
        return
    # Deliveries enqueued before task-identity enforcement lack the payload
    # key; fall back to the document's stored claim so they are not retried
    # until dead-letter (AUD-283).
    expected = payload.get(CLOUD_TASK_NAME) or doc.get(CLOUD_TASK_NAME)
    if expected and doc.get(CLOUD_TASK_NAME) != expected:
        return  # Superseded delivery; a different task owns the document now.
    if expected and _has_active_document_claim(doc, claimed_before=delivery_started):
        return  # A duplicate delivery found the currently active worker's claim.
    raise RuntimeError("Document has no durable terminal outcome; delivery remains retryable.")


# ── OIDC Bearer token verification ───────────────────────────────────────────


ENV_CLOUD_TASKS_SA = "CLOUD_TASKS_SERVICE_ACCOUNT"


def _validate_oidc_claims(id_info: dict, expected_service_accounts: tuple[str, ...] | None = None) -> bool:
    """Helper to validate issuer and email claims on verified OIDC token."""
    if id_info.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        logger.warning("[CloudTasksHandler] OIDC issuer unexpected: %s", id_info.get("iss"))
        return False

    allowed = tuple(sa.strip() for sa in (expected_service_accounts or ()) if sa and sa.strip())
    if not allowed:
        default_sa = getattr(settings, ENV_CLOUD_TASKS_SA, "").strip()
        allowed = (default_sa,) if default_sa else ()

    if allowed:
        if id_info.get("email") not in allowed or not id_info.get("email_verified"):
            logger.warning("[CloudTasksHandler] OIDC caller is not an authorized service account.")
            return False
    elif not settings.DEBUG:
        logger.warning("[CloudTasksHandler] CLOUD_TASKS_SERVICE_ACCOUNT must be configured in production.")
        return False

    return True


def _expand_audiences(audience: str | list[str]) -> list[str]:
    raw_audiences = [audience] if isinstance(audience, str) else list(audience)
    audiences: list[str] = []
    for aud in raw_audiences:
        if not aud:
            continue
        stripped = aud.rstrip("/")
        if stripped not in audiences:
            audiences.append(stripped)
        with_slash = f"{stripped}/"
        if with_slash not in audiences:
            audiences.append(with_slash)
    return audiences


def _verify_token_candidates(
    token: str, audiences: list[str], expected_service_accounts: tuple[str, ...] | None = None
) -> bool:
    try:
        import google.auth.transport.requests
        import google.oauth2.id_token

        request_obj = google.auth.transport.requests.Request()
        for aud in audiences:
            try:
                id_info = google.oauth2.id_token.verify_oauth2_token(token, request_obj, aud)
                if _validate_oidc_claims(id_info, expected_service_accounts=expected_service_accounts):
                    return True
            except Exception as aud_err:
                logger.debug("[CloudTasksHandler] Candidate audience '%s' rejected: %s", aud, aud_err)

        logger.warning("[CloudTasksHandler] OIDC verification failed for all candidate audiences.")
        return False
    except Exception as exc:
        logger.warning("[CloudTasksHandler] OIDC verification failed: %s", exc)
        return False


def _verify_oidc_token(
    request: HttpRequest,
    audience: str | list[str],
    *,
    expected_service_accounts: tuple[str, ...] | None = None,
) -> bool:
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
    audiences = _expand_audiences(audience)
    return _verify_token_candidates(token, audiences, expected_service_accounts=expected_service_accounts)


def _is_ip_in_google_cidrs(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        for cidr in GOOGLE_TASKS_IP_CIDRS:
            if ip in ipaddress.ip_network(cidr, strict=False):
                return True
    except ValueError:
        pass
    return False


def _extract_candidate_ips(request: HttpRequest) -> list[str]:
    raw_remote = request.META.get("REMOTE_ADDR", "").strip()
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate_ips: list[str] = []
    if raw_remote:
        candidate_ips.append(raw_remote)

    if not x_forwarded_for:
        return candidate_ips

    is_private = True
    if raw_remote:
        try:
            is_private = ipaddress.ip_address(raw_remote).is_private
        except ValueError:
            is_private = True

    if is_private:
        candidate_ips.extend([ip.strip() for ip in x_forwarded_for.split(",") if ip.strip()])
    return candidate_ips


def _verify_source_ip(request: HttpRequest) -> bool:
    """
    Verify the request source IP is within known Google Cloud Tasks CIDRs.
    Always returns True in DEBUG mode.
    """
    if settings.DEBUG:
        return True

    for ip_str in _extract_candidate_ips(request):
        if _is_ip_in_google_cidrs(ip_str):
            return True

    raw_remote = request.META.get("REMOTE_ADDR", "").strip()
    clean_remote = str(raw_remote).replace("\r", "").replace("\n", "")[:64]
    logger.warning("[CloudTasksHandler] Request from unrecognised IP (remote: %s)", clean_remote)
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
        if not worker_url and not settings.DEBUG:
            logger.error("[CloudTasksHandler] WORKER_URL must be configured in production.")
            return HttpResponse("Worker routing is not configured", status=503)

        if not _verify_oidc_token(request, [worker_url]):
            return HttpResponse("Unauthorized", status=401)

        if not _verify_source_ip(request):
            # WARNING: Do NOT return 4xx/5xx here — Cloud Tasks retries on non-2xx,
            # causing a retry storm. The OIDC token check above is the real security
            # gate. Log the unrecognised IP for monitoring and proceed.
            raw_ip = (
                str(request.META.get("HTTP_X_FORWARDED_FOR", request.META.get("REMOTE_ADDR", ""))).split(",")[0].strip()
            )
            try:
                valid_ip = str(ipaddress.ip_address(raw_ip))
            except ValueError:
                valid_ip = "[INVALID_IP_FORMAT]"
            logger.warning(
                "[CloudTasksHandler] Proceeding despite unrecognised IP: %s — OIDC token already verified.",
                valid_ip,
            )

        # ── Dispatch ──────────────────────────────────────────────────────
        handler = TASK_REGISTRY.get(task_name)
        if handler is None:
            # SEC-LOG: Sanitize task_name before logging to prevent log injection
            # via crafted newline or control characters in the URL path segment.
            import re as _re

            safe_task_name = _re.sub(r"\W", "_", task_name)[:64]
            logger.error("[CloudTasksHandler] Unknown task name: '%s'", safe_task_name)
            return JsonResponse({"error": "Unknown task."}, status=404)

        body = request.body
        if body and len(body) > 1_048_576:
            return JsonResponse({"error": "Payload exceeds 1MB limit"}, status=400)

        try:
            payload = json.loads(body) if body else {}
        except json.JSONDecodeError:
            return JsonResponse({"error": "Invalid JSON payload"}, status=400)

        if not isinstance(payload, dict):
            return JsonResponse({"error": "Task payload must be a JSON object"}, status=400)

        logger.info("[CloudTasksHandler] Dispatching task '%s' with payload keys: %s", task_name, list(payload.keys()))

        from extractor import surreal_db

        token = surreal_db.document_task_name.set(_document_task_identity(task_name, payload))
        try:
            delivery_started = timezone.now()
            handler(payload)
            _confirm_document_outcome(task_name, payload, delivery_started=delivery_started)
        except Exception:
            logger.exception("[CloudTasksHandler] Task '%s' raised an unhandled exception", task_name)
            # A normal pipeline failure is persisted by the handler and returns normally.
            # An escaping exception means durable completion was not confirmed.
            return JsonResponse(
                {"status": "error", "task": task_name, "detail": "Task execution failed; see Cloud Run logs."},
                status=503,
            )
        finally:
            surreal_db.document_task_name.reset(token)

        return JsonResponse({"status": "ok", "task": task_name})


def _unwrap_eventarc_envelope(request, payload: dict) -> tuple[dict | None, str]:
    """Unwrap direct GCS object, Structured CloudEvent (`data`), or Pub/Sub (`message.data`) envelopes."""
    import base64

    event_type = str(request.headers.get("ce-type") or payload.get("type") or "")
    if isinstance(payload.get("data"), dict):
        return payload["data"], event_type
    message = payload.get("message")
    if isinstance(message, dict) and isinstance(message.get("data"), str):
        raw_bytes = base64.b64decode(message["data"], validate=True)
        if len(raw_bytes) > 1024 * 1024:
            raise ValueError("Decoded Pub/Sub Eventarc payload exceeds 1MB limit")
        unwrapped = json.loads(raw_bytes.decode("utf-8"))
        if not isinstance(unwrapped, dict):
            raise ValueError("Decoded Pub/Sub Eventarc payload must be a JSON object")
        return unwrapped, event_type
    return payload, event_type


def _parse_eventarc_payload(request) -> tuple[dict | None, str, JsonResponse | None]:
    """Validate Eventarc request size, parse JSON, and unwrap CloudEvent/PubSub envelopes."""
    if request.body and len(request.body) > 1024 * 1024:
        return None, "", JsonResponse({"error": "Eventarc payload exceeds 1MB limit"}, status=413)
    try:
        payload = json.loads(request.body) if request.body else {}
    except UnicodeDecodeError, json.JSONDecodeError:
        return None, "", JsonResponse({"error": "Invalid Eventarc JSON payload"}, status=400)
    if not isinstance(payload, dict):
        return None, "", JsonResponse({"error": "Eventarc payload must be a JSON object"}, status=400)
    try:
        unwrapped, event_type = _unwrap_eventarc_envelope(request, payload)
    except ValueError:
        return None, "", JsonResponse({"error": "Invalid Eventarc envelope"}, status=400)
    return unwrapped, event_type, None


def _attach_anchor_embeddings(anchors: list[dict], llm_gateway) -> None:
    embedded = 0
    for anc in anchors:
        if embedded >= MAX_ANCHOR_EMBEDDINGS:
            break
        if isinstance(anc, dict) and not anc.get("embedding") and anc.get("quote"):
            emb = llm_gateway.generate_query_embedding(str(anc["quote"]))
            if emb:
                anc["embedding"] = emb
                embedded += 1


def _record_korda_spend(model_used: str, input_tokens: int, output_tokens: int) -> None:
    if not input_tokens and not output_tokens:
        return
    try:
        from datetime import UTC, datetime

        from . import llm_gateway
        from .models import MonthlySpendLog

        cost = llm_gateway.calculate_gemini_cost(model_used, input_tokens, output_tokens)
        if not MonthlySpendLog.add_cost(
            datetime.now(UTC),
            cost,
            in_tok=int(input_tokens),
            out_tok=int(output_tokens),
        ):
            logger.warning("[KORDA Spend] Spend ledger write returned False.")
    except Exception as exc:
        logger.warning("[KORDA Spend] Failed to record spend: %s", exc)


_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,128}$")
_SLUG_SAFE_RE = re.compile(r"^[A-Za-z0-9_.:@-]{1,200}$")
MAX_ANCHORS_PER_OBJECT = 500
MAX_ANCHOR_EMBEDDINGS = 200


def _parse_gcs_event_fields(payload: dict, bucket_name: str, object_name: str) -> dict[str, str]:
    parts = [p for p in object_name.split("/") if p]
    raw_metadata = payload.get("metadata")
    metadata: dict = raw_metadata if isinstance(raw_metadata, dict) else {}
    # The raw/{scope}/{owner}/{slug}/ path is server-controlled; object custom
    # metadata is caller-influenced and must not override it (AUD-281).
    path_owner = parts[2] if len(parts) >= 5 else ""
    owner_id = str(path_owner or metadata.get("owner_id") or "system").strip() or "system"
    doc_slug = str(metadata.get("doc_slug") or (parts[3] if len(parts) >= 5 else object_name)).strip() or object_name
    source_id = str(metadata.get("source_id") or f"{owner_id}:{doc_slug}")
    title = str(metadata.get("title") or (parts[-1] if parts else "") or doc_slug)
    # Validate cheap fields before any LLM spend: malformed values would fail
    # the schemafull provenance writes later and loop Eventarc retries (AUD-340).
    if not _ID_SAFE_RE.match(owner_id) or not _ID_SAFE_RE.match(source_id):
        raise ValueError("Eventarc object metadata produced an invalid owner_id/source_id")
    if not _SLUG_SAFE_RE.match(doc_slug):
        raise ValueError("Eventarc object metadata produced an invalid doc_slug")
    return {
        "owner_id": owner_id,
        "doc_slug": doc_slug[:200],
        "gcs_uri": f"gs://{bucket_name}/{object_name}",
        "mime_type": str(payload.get("contentType") or "application/pdf"),
        "sha256": str(metadata.get("sha256") or ""),
        "source_id": source_id[:128],
        "title": title[:300],
    }


def _digitalize_gcs_object(payload: dict, bucket_name: str, object_name: str) -> dict:
    """Upsert source metadata and extract verbatim anchors from a raw/ GCS object."""
    from . import llm_gateway, surreal_db

    meta = _parse_gcs_event_fields(payload, bucket_name, object_name)
    # BudgetExceededException propagates to the view, which acknowledges the
    # event as deferred (2xx) instead of retrying a permanently-failing delivery.
    llm_gateway.check_budget_and_api_limit()

    extracted = llm_gateway.extract_anchors_from_gcs_uri(
        gcs_uri=meta["gcs_uri"], mime_type=meta["mime_type"], doc_slug=meta["doc_slug"]
    )
    _record_korda_spend(
        str(extracted.get("model_used") or llm_gateway.MODEL_TIER_LITE),
        int(extracted.get("input_tokens") or 0),
        int(extracted.get("output_tokens") or 0),
    )
    anchors = (extracted.get("anchors") or [])[:MAX_ANCHORS_PER_OBJECT]
    _attach_anchor_embeddings(anchors, llm_gateway)
    version = surreal_db.get_next_source_version(meta["owner_id"], meta["doc_slug"])

    saved_source = surreal_db.upsert_source(
        source_id=meta["source_id"],
        doc_slug=meta["doc_slug"],
        owner_id=meta["owner_id"],
        title=meta["title"],
        mime_type=meta["mime_type"],
        gcs_uri=meta["gcs_uri"],
        sha256=meta["sha256"],
        version=version,
    )
    # upsert_source may converge a retry onto the existing version; anchors
    # must be written under the version actually persisted (AUD-264/285).
    persisted_version = int(saved_source.get("version") or version)
    if anchors:
        surreal_db.create_anchors_with_provenance(
            source_id=meta["source_id"],
            doc_slug=meta["doc_slug"],
            owner_id=meta["owner_id"],
            version=persisted_version,
            anchors=anchors,
        )

    logger.info(
        "[EventarcGCSRaw] Digitalized %s v%d (%d anchors) for owner=%s",
        meta["gcs_uri"],
        persisted_version,
        len(anchors),
        meta["owner_id"],
    )
    return {
        "status": "accepted",
        "bucket": bucket_name,
        "name": object_name,
        "source_id": meta["source_id"],
        "version": persisted_version,
        "anchors_created": len(anchors),
    }


def _build_eventarc_audiences(request) -> list[str]:
    worker_url = (getattr(settings, "WORKER_URL", "") or "").strip()
    candidates = [
        worker_url,
        f"{worker_url.rstrip('/')}{request.path}" if worker_url else "",
        request.build_absolute_uri(request.path),
    ]
    return [url for url in candidates if url]


def _eventarc_service_accounts() -> tuple[str, ...]:
    import os

    candidates = (
        getattr(settings, "EVENTARC_SERVICE_ACCOUNT", "") or os.getenv("EVENTARC_SERVICE_ACCOUNT", ""),
        getattr(settings, ENV_CLOUD_TASKS_SA, "") or os.getenv(ENV_CLOUD_TASKS_SA, ""),
    )
    return tuple(sa.strip() for sa in candidates if sa and sa.strip())


DEFERRED_DIGITALIZATION_COUNTDOWN = 6 * 3600


def _defer_digitalization(payload: dict, bucket_name: str, object_name: str) -> None:
    """Re-drive a budget-deferred Eventarc object on a long countdown.

    A 202 acknowledgement tells Eventarc not to retry, so without this the
    object would never be digitalized and the raw/ lifecycle rule could
    delete it before the monthly budget resets.
    """
    if settings.DEBUG:
        return
    try:
        from extractor import cloud_tasks

        cloud_tasks.enqueue(
            "digitalize_gcs_object",
            {"bucket": bucket_name, "name": object_name, "event": payload},
            countdown=DEFERRED_DIGITALIZATION_COUNTDOWN,
        )
    except Exception as exc:
        logger.warning(
            "[EventarcGCSRaw] Failed to defer digitalization of gs://%s/%s: %s", bucket_name, object_name, exc
        )


def digitalize_gcs_object_task(task_payload: dict) -> None:
    """Cloud Tasks handler that retries a budget-deferred GCS digitalization."""
    from extractor import llm_gateway

    event = task_payload.get("event")
    bucket_name = str(task_payload.get("bucket") or "")
    object_name = str(task_payload.get("name") or "")
    if not isinstance(event, dict) or not bucket_name or not object_name:
        logger.warning("[EventarcGCSRaw] Deferred digitalization task has an invalid payload; dropping.")
        return
    try:
        _digitalize_gcs_object(event, bucket_name, object_name)
    except llm_gateway.BudgetExceededException:
        if settings.DEBUG:
            return
        try:
            from extractor import cloud_tasks

            cloud_tasks.enqueue(
                "digitalize_gcs_object",
                {"bucket": bucket_name, "name": object_name, "event": event},
                countdown=DEFERRED_DIGITALIZATION_COUNTDOWN,
            )
        except Exception as exc:
            logger.warning(
                "[EventarcGCSRaw] Failed to re-defer digitalization of gs://%s/%s: %s", bucket_name, object_name, exc
            )


def _digitalize_or_error_response(payload, bucket_name, object_name):
    """Run Eventarc digitalization, mapping outcomes to the receiver contract."""
    from extractor import llm_gateway

    try:
        return _digitalize_gcs_object(payload, bucket_name, object_name), None
    except llm_gateway.BudgetExceededException as exc:
        # Acknowledge (202) so Eventarc does not retry a permanently-failing
        # delivery; the deferred Cloud Task re-drives it after the budget
        # window instead of losing the object to the raw/ lifecycle rule.
        logger.warning("[EventarcGCSRaw] Monthly budget exceeded for gs://%s/%s: %s", bucket_name, object_name, exc)
        _defer_digitalization(payload, bucket_name, object_name)
        return None, JsonResponse({"status": "deferred", "reason": "monthly-budget-exceeded"}, status=202)
    except ValueError as exc:
        logger.warning("[EventarcGCSRaw] Permanent failure for gs://%s/%s: %s", bucket_name, object_name, exc)
        return None, JsonResponse({"error": "Invalid Eventarc digitalization request."}, status=400)
    except Exception as exc:
        logger.exception("[EventarcGCSRaw] Digitalization failed for gs://%s/%s: %s", bucket_name, object_name, exc)
        return None, JsonResponse({"error": "Eventarc digitalization failed; retryable."}, status=503)


@method_decorator(csrf_exempt, name="dispatch")
class EventarcGCSRawView(View):
    """OIDC-verified Eventarc receiver scoped strictly to raw/ GCS uploads."""

    http_method_names = ["post"]

    def post(self, request):
        if not settings.DEBUG and not _verify_oidc_token(
            request,
            _build_eventarc_audiences(request),
            expected_service_accounts=_eventarc_service_accounts(),
        ):
            return JsonResponse({"error": "Unauthorized Eventarc invocation."}, status=401)

        payload, event_type, error_response = _parse_eventarc_payload(request)
        if error_response is not None or payload is None:
            return error_response

        # Empty event_type is allowed: Pub/Sub push deliveries carry the
        # CloudEvent type in message attributes, not the body, so finalized
        # objects arrive without it. Non-empty non-finalized types are ignored
        # (AUD-284 — empty type is an accepted, OIDC-gated allowance).
        if event_type and event_type != "google.cloud.storage.object.v1.finalized":
            return JsonResponse({"status": "ignored", "reason": "non-finalized-event"})

        object_name = str(payload.get("name") or "")
        bucket_name = str(payload.get("bucket") or "")
        configured_bucket = (getattr(settings, "GS_BUCKET_NAME", "") or "").strip()
        if not object_name.startswith("raw/") or ".." in object_name.split("/") or not bucket_name:
            return JsonResponse({"status": "ignored", "reason": "non-raw-prefix"})
        if configured_bucket and bucket_name != configured_bucket:
            return JsonResponse({"status": "ignored", "reason": "unrecognized-bucket"})

        result, error_response = _digitalize_or_error_response(payload, bucket_name, object_name)
        if error_response is not None:
            return error_response
        return JsonResponse(result)
