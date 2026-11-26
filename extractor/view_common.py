import json
import logging
from datetime import datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.serializers.json import DjangoJSONEncoder
from django.http import JsonResponse
from django.utils import timezone
from django.utils.safestring import mark_safe

from extractor import surreal_db
from extractor.utils import (
    get_client_ip,
    render_markdown_to_html,
)

VIEWS_LOGGER_NAME = "extractor.views"
logger = logging.getLogger(VIEWS_LOGGER_NAME)

ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%SZ"

DOCUMENT_NOT_FOUND_MSG = "Document not found."

ROUTE_DOCUMENT_DETAIL = "document_detail"


class AetherJSONEncoder(DjangoJSONEncoder):
    """Custom JSON encoder that gracefully handles SurrealDB RecordIDs and custom objects."""

    def default(self, obj):
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def parse_datetime(val):
    if not val:
        return timezone.now()
    if isinstance(val, datetime):
        return val
    try:
        return datetime.strptime(val, ISO_8601_FORMAT).replace(tzinfo=timezone.UTC)
    except ValueError, TypeError:
        try:
            from django.utils.dateparse import parse_datetime as django_parse

            parsed = django_parse(val)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.UTC)
                return parsed
        except ValueError, TypeError, AttributeError:
            pass
    return timezone.now()


def format_datetime(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.UTC)
    return dt.strftime(ISO_8601_FORMAT)


def _extract_clean_doc_uuid(doc_or_id: Any) -> str:
    """Safely extract clean UUID string from dict, model, RecordID, or prefixed string."""
    if not doc_or_id:
        return ""
    if hasattr(doc_or_id, "uuid"):
        raw = str(doc_or_id.uuid)
    elif hasattr(doc_or_id, "doc_uuid"):
        raw = str(doc_or_id.doc_uuid)
    elif hasattr(doc_or_id, "id"):
        raw = str(doc_or_id.id)
    elif isinstance(doc_or_id, dict):
        raw = str(doc_or_id.get("doc_uuid") or doc_or_id.get("uuid") or doc_or_id.get("id") or "")
    else:
        raw = str(doc_or_id).strip()
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    return raw.strip()


def _parse_user_query_ids(needed_ids: set[str], pk_field: Any) -> list[Any]:
    """Parse candidate user string IDs into valid primary key lookup types."""
    query_ids = []
    has_to_python = hasattr(pk_field, "to_python")
    for uid_str in needed_ids:
        if not has_to_python:
            query_ids.append(uid_str)
            continue
        try:
            query_ids.append(pk_field.to_python(uid_str))
        except Exception as exc:
            logger.debug("[Users Map] Failed to coerce user ID '%s' with pk field to_python: %s", uid_str, exc)
    return query_ids


def _build_users_map(user_ids=None, fallback_user=None) -> dict:
    """
    Build a mapping of {str(user_id): user_obj} targeting only requested user_ids
    instead of bulk-loading all users in the database.
    """
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    users_map = {}
    if fallback_user and getattr(fallback_user, "is_authenticated", False):
        users_map[str(fallback_user.id)] = fallback_user
        actor_id = getattr(fallback_user, "actor_id", None)
        if actor_id:
            users_map[str(actor_id)] = fallback_user

    if user_ids is None:
        for u in user_model.objects.all():
            users_map[str(u.id)] = u
        return users_map

    needed_ids = {str(uid) for uid in user_ids if uid and str(uid) not in users_map}
    if not needed_ids:
        return users_map

    try:
        query_ids = _parse_user_query_ids(needed_ids, user_model._meta.pk)
        if query_ids:
            for pk_val, user_obj in user_model.objects.in_bulk(query_ids).items():
                users_map[str(pk_val)] = user_obj
    except Exception as exc:
        logger.debug("[Users Map] Scoped user query skipped: %s", exc)

    return users_map


def _get_offline_surreal_doc(doc_uuid: str | None):
    from extractor.models import SourceDocument

    try:
        return SourceDocument.objects.get(uuid=doc_uuid)
    except SourceDocument.DoesNotExist, ValueError, TypeError, ValidationError:
        logger.warning("[SurrealDB Wrapper] SourceDocument with doc_uuid=%s not found in database.", doc_uuid)
        return None


def _build_synthetic_surreal_doc(d: dict, users_map: dict | None = None) -> SimpleNamespace:
    from extractor.models import SourceDocument

    doc_obj = SimpleNamespace()
    doc_uuid = d.get("doc_uuid") or d.get("id")
    doc_obj.id = doc_uuid
    doc_obj.uuid = doc_uuid
    doc_obj.doc_uuid = doc_uuid
    doc_obj.file = d.get("file")
    doc_obj.file_hash = d.get("file_hash")
    doc_obj.original_filename = d.get("original_filename")
    doc_obj.title = d.get("title")
    doc_obj.author = d.get("author")
    doc_obj.language = d.get("language")
    doc_obj.document_type = d.get("document_type")
    doc_obj.status = d.get("status")
    doc_obj.cost_usd = Decimal(str(d.get("cost_usd") if d.get("cost_usd") is not None else 0.0))
    doc_obj.page_count = d.get("page_count") if d.get("page_count") is not None else 0
    doc_obj.input_tokens = d.get("input_tokens") if d.get("input_tokens") is not None else 0
    doc_obj.output_tokens = d.get("output_tokens") if d.get("output_tokens") is not None else 0
    doc_obj.raw_markdown = d.get("raw_markdown")
    doc_obj.refined_markdown = d.get("refined_markdown")
    doc_obj.yaml_metadata = d.get("yaml_metadata")
    doc_obj.qa_dataset = d.get("qa_dataset")
    doc_obj.semantic_signature = d.get("semantic_signature")
    doc_obj.publisher = d.get("publisher") or ""
    doc_obj.publication_year = str(d.get("publication_year") or "")
    doc_obj.license_type = d.get("license_type") or ""
    doc_obj.doi = d.get("doi") or ""
    doc_obj.error_message = d.get("error_message")
    doc_obj.retry_count = d.get("retry_count", 0)

    doc_obj.created_at = parse_datetime(d.get("created_at"))
    doc_obj.updated_at = parse_datetime(d.get("updated_at"))
    doc_obj.expires_at = parse_datetime(d.get("expires_at")) if d.get("expires_at") else None

    uid = d.get("uploaded_by_id")
    doc_obj.uploaded_by_id = uid
    doc_obj.uploaded_by = users_map.get(uid) if (users_map and uid in users_map) else None

    status_display_map = dict(SourceDocument.STATUS_CHOICES)
    doc_obj.get_status_display = lambda: status_display_map.get(doc_obj.status, str(doc_obj.status or "").title())
    return doc_obj


def _doc_owner_id(doc: Any) -> str | None:
    """Return the uploaded_by_id owner key from a raw dict or wrapped document."""
    if isinstance(doc, dict):
        return doc.get("uploaded_by_id")
    return getattr(doc, "uploaded_by_id", None)


def _wrap_surreal_doc(d, users_map=None):
    if not d:
        return None
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        offline_doc = _get_offline_surreal_doc(d.get("doc_uuid") or d.get("id"))
        if offline_doc is not None:
            return offline_doc

    return _build_synthetic_surreal_doc(d, users_map)


def _render_sanitized_markdown(markdown_text: str) -> str:
    """Render Markdown only after render_markdown_to_html applies HTML sanitization."""
    return mark_safe(render_markdown_to_html(markdown_text))  # nosec B308 B703 # nosem


def _is_budget_exceeded(user, actor_id: str | None = None) -> bool:
    """
    Lightweight budget gate that only reads Django ORM tables — no SurrealDB.
    Safe to call from any view, including in test environments.
    Returns True when the authenticated user has consumed their monthly AI budget.
    Staff / superusers are never budget-gated.
    """
    if user.is_staff or user.is_superuser:
        return False
    try:
        from decimal import Decimal

        from django.utils import timezone

        from extractor.models import MonthlySpendLog, SystemSettings

        settings_obj = SystemSettings.get_settings()
        budget_cap = Decimal(str(settings_obj.monthly_budget_usd or 10.0))
        if budget_cap <= 0:
            return False

        now = timezone.now()
        monthly_logged = MonthlySpendLog.total_for_month(now.year, now.month)

        # Add live spend from active SurrealDB documents if available
        monthly_live = Decimal(0)
        try:
            from datetime import UTC, datetime

            from extractor import surreal_db as _sdb

            first_of_month = datetime(now.year, now.month, 1, tzinfo=UTC)
            monthly_live = _sdb.get_user_total_spend(actor_id or str(user.id), since=first_of_month)
        except Exception as err:
            # Fail closed in production: an unreadable spend source must not
            # let uploads/quiz generation slip past the budget pre-check.
            # The worker-side gate remains the authoritative enforcement.
            if not getattr(settings, "SURREALDB_OFFLINE", False):
                logger.warning("[Budget Check] SurrealDB live-spend read failed; denying request: %s", err)
                return True
            logger.debug("[Budget Check] SurrealDB query bypassed, relying on MonthlySpendLog: %s", err)

        return (monthly_live + monthly_logged) >= budget_cap
    except Exception as exc:
        if getattr(settings, "SURREALDB_OFFLINE", False):
            logger.warning("[Budget Check] Offline budget evaluation failed; allowing local-only request: %s", exc)
            return False
        logger.exception("[Budget Check] Budget evaluation failed; denying production request")
        return True


def _enforce_distributed_rate_limit(
    surreal_key: str,
    cache_key: str,
    max_requests: int,
    window_seconds: int = 60,
) -> bool:
    """Enforce rate limit via SurrealDB in production, or local cache in debug/offline mode."""
    if not getattr(settings, "DEBUG", False) and not getattr(settings, "SURREALDB_OFFLINE", False):
        try:
            from extractor import surreal_db

            return bool(
                surreal_db.check_rate_limit_atomic(
                    surreal_key,
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                )
            )
        except Exception as surreal_err:
            logger.warning("[RateLimit] SurrealDB distributed rate limit error for %s: %s", surreal_key, surreal_err)
            return False

    from django.core.cache import cache

    try:
        if cache.add(cache_key, 1, window_seconds):
            rl_count = 1
        else:
            try:
                rl_count = cache.incr(cache_key)
            except ValueError:
                cache.set(cache_key, 1, window_seconds)
                rl_count = 1
        return rl_count <= max_requests
    except Exception as exc:
        logger.debug("[RateLimit] Cache backend error, falling back to SurrealDB: %s", exc)
        try:
            from extractor import surreal_db

            return bool(
                surreal_db.check_rate_limit_atomic(
                    surreal_key,
                    max_requests=max_requests,
                    window_seconds=window_seconds,
                )
            )
        except Exception as surreal_err:
            logger.warning("[RateLimit] SurrealDB fallback error for %s: %s", surreal_key, surreal_err)
            return False


def _safe_metadata_source_link(value: object) -> str:
    """Return only absolute HTTP(S) metadata links safe for a document anchor."""
    source_link = str(value or "").strip()
    parsed_url = urlparse(source_link)
    if parsed_url.scheme in {"http", "https"} and parsed_url.netloc:
        return source_link
    return ""


def get_request_actor_id(request) -> str:
    """Return the durable Supabase subject in production, not a local Django PK."""
    if not request.user.is_authenticated:
        return ""
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return str(request.user.id)
    return str(request.session.get("supabase_user_id") or request.user.id)


def _invalidate_doc_rag_caches(doc_uuid: str, actor_id: str | None = None) -> None:
    """Purge cached RAG answers derived from a document's prior content.

    Production document writes go through SurrealDB, so the Django post_save
    cache-invalidation signal cannot fire; callers must invoke this explicitly
    after content changes (edit, retry, restart). Failure-isolated: cache
    purge errors are logged, never raised into the request path.
    """
    try:
        surreal_db.delete_rag_cache_for_document(str(doc_uuid))
        if actor_id:
            surreal_db.kv_cache_delete_pattern(f"{surreal_db.RAG_SEARCH_CACHE_PREFIX}{actor_id}:")
    except Exception as exc:
        logger.warning("[RAGCache] Invalidation failed for %s: %s", doc_uuid, exc)


def _record_view_audit(request, action, details: str, document=None, ip: str | None = None) -> None:
    """Helper to record standardized view-level audit trail events without repetitive boilerplate."""
    from extractor.utils import AuditEvent, log_audit_event

    log_audit_event(
        AuditEvent(
            action=action,
            user=request.user,
            actor_id=get_request_actor_id(request),
            document=document,
            details=details,
            ip_address=ip or get_client_ip(request),
        )
    )


def _get_authorized_wrapped_doc(request, doc_uuid, allow_unowned: bool = False):
    """
    Fetch a raw SurrealDB document by UUID, wrap it with Django user mappings,
    and verify user ownership / staff authorization.
    Returns (raw_doc, doc_wrapped, is_authorized, users_map).
    """
    raw_doc = surreal_db.get_document(doc_uuid)
    if not raw_doc:
        return None, None, False, {}

    uid = raw_doc.get("uploaded_by_id")
    users_map = _build_users_map([uid], fallback_user=request.user)
    doc = _wrap_surreal_doc(raw_doc, users_map)

    actor_id = get_request_actor_id(request)
    is_owner = (doc.uploaded_by is not None and doc.uploaded_by == request.user) or (
        getattr(doc, "uploaded_by_id", None) and doc.uploaded_by_id == actor_id
    )
    # "Unowned" must key off the recorded owner id, not the resolved User:
    # uploaded_by is None whenever the owner key cannot be mapped to a Django
    # user (e.g. another tenant's Supabase subject), which would otherwise
    # make allow_unowned=True grant access to every other user's document.
    is_unowned = not raw_doc.get("uploaded_by_id")
    is_authorized = (
        request.user.is_staff or request.user.is_superuser or (allow_unowned and is_unowned) or is_owner
    )
    return raw_doc, doc, is_authorized, users_map


ERR_INVALID_JSON_PAYLOAD = "Invalid JSON payload."

ERR_SERVICE_UNAVAILABLE = "Service temporarily unavailable."
ERR_RATE_LIMIT = "Rate limit exceeded."

MAX_JSON_BODY_BYTES = 1024 * 1024


def _parse_json_dict_body(request) -> tuple[dict | None, JsonResponse | None]:

    content_length = request.META.get("CONTENT_LENGTH", "")
    if content_length.isdigit() and int(content_length) > MAX_JSON_BODY_BYTES:
        return None, JsonResponse({"error": "Request body too large."}, status=413)

    try:
        if content_length.isdigit():
            raw_body = request.body
        else:
            # Chunked or missing Content-Length: bound the read ourselves so the
            # 1 MiB cap still applies instead of relying on DATA_UPLOAD_MAX_MEMORY_SIZE.
            raw_body = request.read(MAX_JSON_BODY_BYTES + 1)
            if len(raw_body) > MAX_JSON_BODY_BYTES:
                return None, JsonResponse({"error": "Request body too large."}, status=413)
        body = json.loads(raw_body) if raw_body else {}
    except UnicodeDecodeError, json.JSONDecodeError:
        return None, JsonResponse({"error": ERR_INVALID_JSON_PAYLOAD}, status=400)
    if not isinstance(body, dict):
        return None, JsonResponse({"error": "JSON object required."}, status=400)
    return body, None


def _parse_positive_int(raw: Any, *, default: int = 5, min_val: int = 1, max_val: int = 30) -> int | None:
    if raw is None:
        return default
    if isinstance(raw, bool):
        return None
    try:
        val = int(raw)
    except TypeError, ValueError, OverflowError:
        return None
    return max(min_val, min(val, max_val))
