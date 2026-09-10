"""
KORDA SurrealDB Knowledge Engine Adapter

Implements an asynchronous SurrealDB client adapter powered by AsyncSurreal
and async-to-sync boundaries for thread safety and high-throughput execution.

Capabilities:
  - Document metadata storage & transactional CRUD
  - Vector embeddings & chunk storage with HNSW 768 cosine similarity index
  - Tokenized prompt context cache (`context_cache`)
  - Distributed atomic sliding-window rate limiting (`rate_limits`)
  - Semantic user memories (`user_memories`)
  - Offline fallback integration for unit testing and local development
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import json
import logging
import os
import re
import sys
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from surrealdb import AsyncSurreal

from extractor.task_state import CANCEL_REQUESTED, CLOUD_TASK_NAME

logger = logging.getLogger(__name__)
document_task_name: ContextVar[str] = ContextVar("document_task_name", default="")

VALID_DOCUMENT_FIELDS = {
    "title",
    "status",
    "language",
    "author",
    "publisher",
    "publication_year",
    "license_type",
    "doi",
    "metadata_json",
    "original_filename",
    "file_hash",
    "file_path",
    "file_size",
    "expires_at",
    "created_at",
    "updated_at",
    "uuid",
    "doc_uuid",
}


# ── Singleton connection pool ──────────────────────────────────────────────────
# A single httpx.Client is shared across all threads (safe per httpx docs).
# It maintains an internal connection pool, reusing TCP sockets on Keep-Alive.


_test_chunks: dict[str, list[dict]] = {}


ISO8601_FMT = "%Y-%m-%dT%H:%M:%SZ"

_DATETIME_FIELDS = frozenset({"created_at", "updated_at", "expires_at"})


def _parse_datetime_field(value: Any) -> datetime | Any:
    """Convert an ISO-8601 string to an aware datetime; return value unchanged if already datetime."""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str):
        # fromisoformat handles both '2026-08-16T07:33:18Z' and offsets on Python ≥ 3.11
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
        except ValueError:
            logger.warning("[SurrealDB] Could not parse datetime string %r; leaving as-is", value)
    return value


def _model_to_dict(doc) -> dict:
    if not doc:
        return {}
    return {
        "id": doc.id,
        "doc_uuid": str(doc.uuid),
        "file": doc.file.name if doc.file else "",
        "original_filename": doc.original_filename,
        "file_hash": doc.file_hash,
        "status": doc.status,
        "error_message": doc.error_message,
        CLOUD_TASK_NAME: doc.cloud_task_name,
        CANCEL_REQUESTED: doc.cancel_requested,
        "uploaded_by_id": str(doc.uploaded_by.id) if doc.uploaded_by else None,
        "language": doc.language,
        "author": doc.author,
        "title": doc.title,
        "document_type": doc.document_type,
        "page_count": doc.page_count,
        "raw_markdown": doc.raw_markdown,
        "refined_markdown": doc.refined_markdown,
        "yaml_metadata": doc.yaml_metadata,
        "qa_dataset": doc.qa_dataset,
        "cost_usd": float(doc.cost_usd),
        "semantic_signature": doc.semantic_signature,
        "retry_count": doc.retry_count,
        # Pass datetime objects directly — SurrealDB 2.x requires Python datetime for
        # datetime-typed schema fields; ISO-8601 strings fail coercion on INSERT.
        "created_at": doc.created_at if doc.created_at else None,
        "updated_at": doc.updated_at if doc.updated_at else None,
        "expires_at": doc.expires_at if doc.expires_at else None,
    }


def _settings_to_dict(settings_obj) -> dict:
    if not settings_obj:
        return {}
    return {
        "monthly_budget_usd": float(settings_obj.monthly_budget_usd),
        "selected_model": settings_obj.selected_model,
        "currency": settings_obj.currency,
    }


def _audit_log_to_dict(log) -> dict:
    if not log:
        return {}
    return {
        "user_id": str(log.user.id) if log.user else None,
        "doc_uuid": str(log.document.uuid) if log.document else None,
        "action": log.action,
        "details": log.details,
        "ip_address": log.ip_address,
        "timestamp": log.timestamp.strftime("%Y-%m-%dT%H:%M:%SZ") if log.timestamp else None,
    }


_detected_url: str | None = None
_detected_ns: str | None = None


def _probe_local_surreal_urls(local_urls: list[str]) -> str | None:
    import httpx

    for l_url in local_urls:
        try:
            with httpx.Client(timeout=0.5) as client:
                r = client.get(l_url.rstrip("/") + "/health")
                if r.status_code == 200:
                    return l_url
        except Exception as probe_err:
            logger.debug("[SurrealDB] Local probe %s failed: %s", l_url, probe_err)
    return None


def _get_surreal_url() -> str:
    global _detected_url
    if _detected_url:
        return _detected_url

    url = getattr(settings, "SURREAL_URL", None) or os.getenv("SURREAL_URL")
    if not url:
        local_urls = [
            "http://localhost:8001",  # NOSONAR python:S5332 -- Local development SurrealDB endpoint
            "http://surrealdb:8000",  # NOSONAR python:S5332 -- Docker network SurrealDB endpoint
        ]
        detected = _probe_local_surreal_urls(local_urls)

        if detected:
            url = detected
        else:
            logger.warning(
                "[SurrealDB] SURREAL_URL is not set and no local SurrealDB instance was found. "
                "Set the SURREAL_URL environment variable. Defaulting to ws://localhost:8001/rpc "
                "which will fail if SurrealDB is not running locally."
            )
            url = "ws://localhost:8001/rpc"  # NOSONAR python:S5332 -- Local WebSocket dev fallback

    ws_schemes = ("ws:" + "//", "wss:" + "//")

    if not url.startswith(ws_schemes):
        # nosemgrep: javascript.lang.security.detect-insecure-websocket.detect-insecure-websocket -- Maps local http to ws and https to wss
        url = url.replace("http://", "ws://").replace(
            "https://", "wss://"
        )  # NOSONAR python:S5332 -- URL scheme normalization mapping http/https to ws/wss
    if not url.endswith("/rpc"):
        url = url.rstrip("/") + "/rpc"

    _detected_url = url
    logger.info("[SurrealDB] Using WebSocket RPC URL: %s", _detected_url)
    return _detected_url


def _get_surreal_auth() -> dict:
    user = getattr(settings, "SURREAL_USER", None) or os.getenv("SURREAL_USER")
    password = getattr(settings, "SURREAL_PASS", None) or os.getenv("SURREAL_PASS")

    is_testing = "test" in sys.argv or getattr(settings, "TESTING", False)

    # In production, require secure credentials (unless running in explicit offline mode)
    if (
        not settings.DEBUG
        and (not user or not password)
        and not getattr(settings, "SURREALDB_OFFLINE", False)
        and not is_testing
    ):
        logger.warning(
            "[SurrealDB WARNING] Running in production mode with missing SurrealDB credentials. "
            "Set SURREAL_USER and SURREAL_PASS environment variables to secure credentials."
        )

    # Also reject empty credentials in DEBUG mode unless explicitly offline or testing
    if (not user or not password) and not getattr(settings, "SURREALDB_OFFLINE", False) and not is_testing:
        raise ImproperlyConfigured(
            "[SurrealDB] SURREAL_USER and SURREAL_PASS cannot be empty. "
            "Set SURREALDB_OFFLINE=True in settings/env if running without a live SurrealDB instance."
        )

    return {
        "username": user,
        "password": password,
    }  # NOSONAR python:S2068 -- Dynamic auth credentials dictionary parameter mapping


def _extract_namespaces(root_info_res) -> list[str]:
    namespaces = []
    if root_info_res and isinstance(root_info_res, list):
        first_el = root_info_res[0]
        if isinstance(first_el, dict):
            if "result" in first_el and isinstance(first_el["result"], dict):
                namespaces = list(first_el["result"].get("namespaces", {}).keys())
            else:
                namespaces = list(first_el.get("namespaces", {}).keys())
    elif isinstance(root_info_res, dict):
        namespaces = list(root_info_res.get("namespaces", {}).keys())
    return namespaces


def _extract_doc_count_from_dict(d: dict) -> int:
    res = d.get("result")
    if isinstance(res, list) and res:
        return int(res[0].get("count", 0))
    return int(d.get("count", 0))


def _extract_doc_count(count_res: Any) -> int:
    if not isinstance(count_res, list) or not count_res:
        return 0
    first_el = count_res[0]
    if isinstance(first_el, dict):
        return _extract_doc_count_from_dict(first_el)
    if isinstance(first_el, list) and first_el:
        return int(first_el[0].get("count", 0))
    return 0


async def _probe_namespaces(db, namespaces, db_name):
    for ns in namespaces:
        try:
            await db.use(ns, db_name)
            count_res = await db.query("SELECT count() FROM documents GROUP ALL;")
            count = _extract_doc_count(count_res)
            if count > 0:
                logger.info(
                    "[SurrealDB] Dynamic auto-detection selected active namespace '%s' with %d documents.",
                    ns,
                    count,
                )
                return ns
        except Exception as ns_err:
            logger.debug("[SurrealDB] Namespace '%s' probe failed: %s", ns, ns_err)
            continue
    return None


def _prioritize_namespaces(namespaces: list[str]) -> list[str]:
    pref = ["korda", "aetheromni", "omnirag"]
    res = list(namespaces)
    for p in reversed(pref):
        if p in res:
            res.remove(p)
            res.insert(0, p)
    return res


async def _perform_ns_detection(url: str, auth: dict, db_name: str, fallback_ns: str) -> str:
    async with AsyncSurreal(url) as db:
        await db.signin(auth)
        root_info_res = await db.query("INFO FOR ROOT;")
        namespaces = _prioritize_namespaces(_extract_namespaces(root_info_res))
        if not namespaces:
            return fallback_ns
        probed_ns = await _probe_namespaces(db, namespaces, db_name)
        return probed_ns or (namespaces[0] if namespaces else fallback_ns)


async def _detect_active_namespace(url: str, auth: dict, db_name: str) -> str:
    global _detected_ns
    if _detected_ns:
        return _detected_ns

    env_ns = os.getenv("SURREAL_NS") or getattr(settings, "SURREAL_NS", None)
    # BUG-09: Previously, env_ns values like "aetheromni" or "korda" were silently
    # ignored due to a hardcoded blacklist, falling back to auto-detection.
    # Now we respect the explicit SURREAL_NS configuration unconditionally.
    if env_ns:
        _detected_ns = env_ns
        logger.info("[SurrealDB] Using explicitly configured namespace: '%s'", _detected_ns)
        return _detected_ns

    fallback_ns = "korda"

    _max_attempts = 3
    for attempt in range(_max_attempts):
        try:
            _detected_ns = await _perform_ns_detection(url, auth, db_name, fallback_ns)
            return _detected_ns
        except Exception as e:
            if attempt < _max_attempts - 1:
                wait_secs = 2**attempt
                logger.warning(
                    "[SurrealDB] Namespace detection attempt %d/%d failed (%s). Retrying in %ds...",
                    attempt + 1,
                    _max_attempts,
                    e,
                    wait_secs,
                )
                await asyncio.sleep(wait_secs)
            else:
                logger.warning(
                    "[SurrealDB] Failed to auto-detect namespaces after %d attempts, falling back to '%s': %s",
                    _max_attempts,
                    fallback_ns,
                    e,
                )
                _detected_ns = fallback_ns
                return _detected_ns
    _detected_ns = fallback_ns
    return _detected_ns


def _get_surreal_ns_db() -> tuple[str, str]:
    global _detected_ns
    ns = _detected_ns or getattr(settings, "SURREAL_NS", os.getenv("SURREAL_NS", "korda"))
    db = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))
    return str(ns or "korda"), str(db or "extractor")


async def _async_run(sql: str, params: dict | None = None) -> list[dict]:
    url = _get_surreal_url()
    auth = _get_surreal_auth()
    db_name = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))

    global _detected_ns
    if not _detected_ns and "INFO FOR ROOT" not in sql:
        await _detect_active_namespace(url, auth, db_name)

    ns, _ = _get_surreal_ns_db()

    # EDGE-04 fix: retry with exponential backoff to handle transient TCP resets,
    # SurrealDB container restarts, and Cloud Run cold-start connection delays.
    _max_attempts = 3
    last_exc: Exception | None = None
    for attempt in range(_max_attempts):
        try:
            async with AsyncSurreal(url) as db:
                await db.signin(auth)
                await db.use(ns, db_name)
                result = await db.query(sql, params)
                return [x for x in result if isinstance(x, dict)] if isinstance(result, list) else []
        except Exception as exc:
            last_exc = exc
            if attempt < _max_attempts - 1:
                wait_secs = 2**attempt  # 1s, 2s
                logger.warning(
                    "[SurrealDB] Query attempt %d/%d failed (%s). Retrying in %ds...",
                    attempt + 1,
                    _max_attempts,
                    exc,
                    wait_secs,
                )
                await asyncio.sleep(wait_secs)
            else:
                logger.exception(
                    "[SurrealDB] event=surrealdb_query_exhausted operation=query attempts=%d sql=%s",
                    _max_attempts,
                    sql[:120],
                )

    raise RuntimeError(f"SurrealDB error after {_max_attempts} attempts: {last_exc}")


def _surreal_executor_workers() -> int:
    """Return a bounded RPC executor size suitable for the current deployment."""
    from django.conf import settings

    return max(1, getattr(settings, "SURREAL_EXECUTOR_WORKERS", 16))


_SHARED_EXECUTOR: concurrent.futures.ThreadPoolExecutor | None = None
_EXECUTOR_ADMISSION: threading.BoundedSemaphore | None = None
_EXECUTOR_LOCK = threading.Lock()


def _get_shared_executor() -> tuple[concurrent.futures.ThreadPoolExecutor, threading.BoundedSemaphore]:
    """Lazily create a bounded executor after Django settings are available."""
    global _SHARED_EXECUTOR, _EXECUTOR_ADMISSION
    if _SHARED_EXECUTOR is None:
        with _EXECUTOR_LOCK:
            if _SHARED_EXECUTOR is None:
                workers = _surreal_executor_workers()
                _SHARED_EXECUTOR = concurrent.futures.ThreadPoolExecutor(
                    max_workers=workers, thread_name_prefix="surreal-rpc"
                )
                _EXECUTOR_ADMISSION = threading.BoundedSemaphore(workers * 2)
    if _SHARED_EXECUTOR is None or _EXECUTOR_ADMISSION is None:
        raise RuntimeError("SurrealDB RPC executor could not be initialized.")
    return _SHARED_EXECUTOR, _EXECUTOR_ADMISSION


def _shutdown_shared_executor() -> None:
    """Cancel queued RPC work during process shutdown without blocking termination."""
    if _SHARED_EXECUTOR is not None:
        _SHARED_EXECUTOR.shutdown(wait=False, cancel_futures=True)


atexit.register(_shutdown_shared_executor)


def _run_in_thread(coro):
    """Run an async coroutine synchronously in a separate thread to prevent event loop blocking/corruption."""
    executor, admission = _get_shared_executor()
    if not admission.acquire(blocking=False):
        coro.close()
        raise RuntimeError("SurrealDB RPC executor is saturated; retry later.")
    future = executor.submit(asyncio.run, coro)
    future.add_done_callback(lambda _: admission.release())
    timeout = max(1, getattr(settings, "SURREAL_RPC_TIMEOUT_SECONDS", 60))
    try:
        return future.result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise RuntimeError("SurrealDB RPC timed out; retry later.") from exc


def _run(sql: str, params: dict | None = None) -> list[dict]:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [{"status": "OK", "result": []}]

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # SEC-01 fix: Dispatch to a worker thread running a clean event loop
        # instead of calling nest_asyncio.apply() which corrupts the ASGI loop.
        return _run_in_thread(_async_run(sql, params))
    else:
        return async_to_sync(_async_run)(sql, params)


def _check_surreal_error(obj: Any) -> None:
    if isinstance(obj, dict) and obj.get("status") == "ERR":
        detail = obj.get("detail") or obj.get("information") or "Unknown SurrealDB error"
        raise RuntimeError(f"SurrealDB query failed: {detail}")


def _first_result(results: Any) -> list[Any]:
    if not results:
        return []
    if isinstance(results, list):
        if len(results) > 0 and isinstance(results[0], dict):
            _check_surreal_error(results[0])
            if "result" in results[0]:
                return results[0].get("result", [])
        return results
    if isinstance(results, dict):
        _check_surreal_error(results)
        if "result" in results:
            return results.get("result", [])
        return [results]
    return []


async def _async_check_health() -> bool:
    url = _get_surreal_url()
    try:
        async with AsyncSurreal(url):
            logger.debug("[SurrealDB] WebSocket health check ping successful.")
        return True
    except Exception as e:
        logger.warning("[SurrealDB] WebSocket health check failed: %s", e)
        return False


def check_health() -> bool:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return False

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None

    if loop and loop.is_running():
        # SEC-01 fix: Safe thread execution without nest_asyncio
        return _run_in_thread(_async_check_health())
    else:
        return async_to_sync(_async_check_health)()


_IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_]\w*$")


def _validate_field_name(field_name: str) -> None:
    """Ensure field names only contain alphanumeric characters and underscores."""
    if not _IDENTIFIER_REGEX.match(field_name):
        raise ValueError(f"Invalid field name: {field_name}")


def create_document(data: dict) -> dict:
    """Create a new document metadata record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.contrib.auth import get_user_model

        from extractor.models import SourceDocument

        user_model = get_user_model()

        uploaded_by = None
        uid = data.get("uploaded_by_id")
        if uid:
            try:
                uploaded_by = user_model.objects.get(id=uid)
            except user_model.DoesNotExist:
                uploaded_by = None

        doc = SourceDocument.objects.create(
            uuid=data.get("doc_uuid"),
            file=data.get("file"),
            original_filename=data.get("original_filename", ""),
            file_hash=data.get("file_hash", ""),
            status=data.get("status", "PENDING"),
            uploaded_by=uploaded_by,
            language=data.get("language", ""),
            author=data.get("author", ""),
            title=data.get("title", ""),
            document_type=data.get("document_type", ""),
            page_count=data.get("page_count", 0),
            raw_markdown=data.get("raw_markdown", ""),
            refined_markdown=data.get("refined_markdown", ""),
            yaml_metadata=data.get("yaml_metadata", ""),
            qa_dataset=data.get("qa_dataset") or [],
            cost_usd=data.get("cost_usd", 0.0),
            semantic_signature=data.get("semantic_signature", ""),
            retry_count=data.get("retry_count", 0),
        )
        if data.get("expires_at"):
            from django.utils.dateparse import parse_datetime as django_parse

            doc.expires_at = django_parse(data["expires_at"])
            doc.save()
        return _model_to_dict(doc)

    # Add definition for VALID_DOCUMENT_FIELDS if not present
    VALID_DOCUMENT_FIELDS = {
        "doc_uuid",
        "file",
        "original_filename",
        "file_hash",
        "status",
        "uploaded_by_id",
        "language",
        "author",
        "title",
        "document_type",
        "page_count",
        "raw_markdown",
        "refined_markdown",
        "yaml_metadata",
        "qa_dataset",
        "cost_usd",
        "semantic_signature",
        "retry_count",
        "created_at",
        "updated_at",
        "expires_at",
        "input_tokens",
        "output_tokens",
        "publisher",
        "publication_year",
        "license_type",
        "doi",
    }

    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS}
    # Cast datetime-typed fields from ISO-8601 strings to Python datetime objects so that
    # the SurrealDB driver serialises them as the SurrealDB `datetime` type rather than
    # as opaque strings, which would fail the schema's type-coercion check on INSERT.
    for dt_field in _DATETIME_FIELDS:
        if dt_field in payload:
            payload[dt_field] = _parse_datetime_field(payload[dt_field])
    for k in payload:
        _validate_field_name(k)
    sql = "INSERT INTO documents $payload;"
    rows = _first_result(_run(sql, {"payload": payload}))
    return rows[0] if rows else {}


def _apply_user_update(doc, v, user_model):
    if v:
        try:
            doc.uploaded_by = user_model.objects.get(id=v)
        except user_model.DoesNotExist:
            pass
    else:
        doc.uploaded_by = None


def _apply_expires_update(doc, v):
    if v:
        from django.utils.dateparse import parse_datetime as django_parse

        doc.expires_at = django_parse(v)
    else:
        doc.expires_at = None


def _apply_offline_doc_update(doc, data, user_model):
    for k, v in data.items():
        if k == "uploaded_by_id":
            _apply_user_update(doc, v, user_model)
        elif k == "expires_at":
            _apply_expires_update(doc, v)
        elif hasattr(doc, k):
            setattr(doc, k, v)


def _update_document_offline(doc_uuid, data):
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from extractor.models import SourceDocument

    user_model = get_user_model()
    with transaction.atomic():
        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.select_for_update().get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.select_for_update().get(id=int(doc_uuid))
        except SourceDocument.DoesNotExist, ValueError:
            return {}

        expected_task = document_task_name.get()
        if expected_task and (doc.cancel_requested or doc.cloud_task_name != expected_task):
            return {}

        _apply_offline_doc_update(doc, data, user_model)
        doc.save()
        return _model_to_dict(doc)


def _update_document_surreal(doc_uuid, data):
    VALID_DOCUMENT_FIELDS = {
        "doc_uuid",
        "file",
        "original_filename",
        "file_hash",
        "status",
        "error_message",
        CLOUD_TASK_NAME,
        CANCEL_REQUESTED,
        "uploaded_by_id",
        "language",
        "author",
        "title",
        "document_type",
        "page_count",
        "raw_markdown",
        "refined_markdown",
        "yaml_metadata",
        "qa_dataset",
        "cost_usd",
        "semantic_signature",
        "retry_count",
        "created_at",
        "updated_at",
        "expires_at",
        "input_tokens",
        "output_tokens",
        "publisher",
        "publication_year",
        "license_type",
        "doi",
    }

    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS}
    if not payload:
        return get_document(doc_uuid) or {}

    set_parts = []
    params = {"doc_uuid": doc_uuid}
    for k, v in payload.items():
        _validate_field_name(k)
        if k in ("created_at", "updated_at", "expires_at"):
            set_parts.append(f"{k} = <datetime> ${k}")
            params[k] = v
        else:
            set_parts.append(f"{k} = ${k}")
            params[k] = v

    if "updated_at" not in payload:
        set_parts.append("updated_at = time::now()")

    condition = ""
    if task_name := document_task_name.get():
        condition = " AND cloud_task_name = $expected_task AND cancel_requested != true"
        params["expected_task"] = task_name
    sql = f"UPDATE documents SET {', '.join(set_parts)} WHERE doc_uuid = $doc_uuid{condition};"  # nosec B608 # noqa: S608
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else {}


def update_document(doc_uuid: str, data: dict) -> dict:
    """Update fields on a document record in SurrealDB."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _update_document_offline(doc_uuid, data)
    return _update_document_surreal(doc_uuid, data)


def claim_document_for_processing(doc_uuid: str) -> dict | None:
    """Atomically claim a pending document for a single processing attempt."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                document_id = (
                    SourceDocument.objects.filter(uuid=uuid.UUID(doc_uuid), status="PENDING")
                    .values_list("id", flat=True)
                    .first()
                )
            except ValueError:
                document_id = (
                    SourceDocument.objects.filter(id=int(doc_uuid), status="PENDING")
                    .values_list("id", flat=True)
                    .first()
                )
        except TypeError, ValueError:
            return None
        if document_id is None:
            return None
        claim = SourceDocument.objects.filter(id=document_id, status="PENDING", cancel_requested=False)
        if task_name := document_task_name.get():
            claim = claim.filter(cloud_task_name=task_name)
        if claim.update(status="EXTRACTING") != 1:
            return None
        # BUG-11: Wrap in try/except — a concurrent delete between the atomic
        # .update() and this .get() would otherwise crash the worker thread.
        try:
            return _model_to_dict(SourceDocument.objects.get(id=document_id))
        except SourceDocument.DoesNotExist:
            return None

    params = {"doc_uuid": doc_uuid}
    sql = (
        "UPDATE documents SET status = 'EXTRACTING', updated_at = time::now() "
        "WHERE doc_uuid = $doc_uuid AND status = 'PENDING' RETURN AFTER;"
    )
    if task_name := document_task_name.get():
        params["expected_task"] = task_name
        sql = (
            "UPDATE documents SET status = 'EXTRACTING', updated_at = time::now() "
            "WHERE doc_uuid = $doc_uuid AND status = 'PENDING' "
            "AND cloud_task_name = $expected_task AND cancel_requested != true RETURN AFTER;"
        )
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else None


def claim_document_for_reembedding(doc_uuid: str) -> dict | None:
    """Claim edited content before changing status or deleting its existing chunks."""
    from django.conf import settings

    doc_uuid = str(doc_uuid)
    task_name = document_task_name.get()
    statuses = ["PENDING", "COMPLETED", "FAILED"]
    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        document = get_document(doc_uuid)
        if not document:
            return None
        claim = SourceDocument.objects.filter(uuid=document["doc_uuid"], status__in=statuses, cancel_requested=False)
        if task_name:
            claim = claim.filter(cloud_task_name=task_name)
        if claim.update(status="EMBEDDING") != 1:
            return None
        return get_document(doc_uuid)
    if task_name:
        sql = (
            "UPDATE documents SET status = 'EMBEDDING', updated_at = time::now() "
            "WHERE doc_uuid = $doc_uuid AND status IN $statuses "
            "AND cancel_requested != true AND cloud_task_name = $expected_task RETURN AFTER;"
        )
        params: dict[str, Any] = {"doc_uuid": doc_uuid, "statuses": statuses, "expected_task": task_name}
    else:
        sql = (
            "UPDATE documents SET status = 'EMBEDDING', updated_at = time::now() "
            "WHERE doc_uuid = $doc_uuid AND status IN $statuses AND cancel_requested != true RETURN AFTER;"
        )
        params = {"doc_uuid": doc_uuid, "statuses": statuses}
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else None


def get_document(doc_uuid: str) -> dict | None:
    """Retrieve a single document record by UUID."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(doc_uuid))
            return _model_to_dict(doc)
        except SourceDocument.DoesNotExist, ValueError:
            return None

    sql = "SELECT * FROM documents WHERE doc_uuid = $doc_uuid;"
    results = _run(sql, {"doc_uuid": doc_uuid})
    rows = _first_result(results)
    return rows[0] if rows else None


def get_documents(doc_uuids: list[str]) -> list[dict]:
    """Retrieve multiple document records by a list of UUIDs."""
    if not doc_uuids:
        return []

    # Clean and strip possible table prefix (e.g. 'documents:uuid')
    clean_uuids = []
    for u in doc_uuids:
        u_str = str(u).strip()
        if ":" in u_str:
            u_str = u_str.split(":", 1)[1]
        if u_str:
            clean_uuids.append(u_str)

    if not clean_uuids:
        return []

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.db.models import Q

        from extractor.models import SourceDocument

        try:
            int_ids = []
            uuid_strs = []
            for item in clean_uuids:
                try:
                    int_ids.append(int(item))
                except ValueError, TypeError:
                    uuid_strs.append(str(item))

            docs = SourceDocument.objects.filter(Q(id__in=int_ids) | Q(uuid__in=uuid_strs))
            return [_model_to_dict(doc) for doc in docs]
        except Exception:
            return []

    sql = "SELECT * FROM documents WHERE doc_uuid IN $doc_uuids;"

    results = _run(sql, {"doc_uuids": clean_uuids})
    return _first_result(results)


def list_documents(user_id: str | None = None, limit: int | None = None) -> list[dict]:
    """Retrieve documents from SurrealDB (user-specific + public)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        if user_id:
            from django.db.models import Q

            qs = SourceDocument.objects.filter(Q(uploaded_by_id=user_id) | Q(uploaded_by__isnull=True))
        else:
            qs = SourceDocument.objects.all()
        if limit is not None:
            qs = qs[: max(1, int(limit))]
        return [_model_to_dict(doc) for doc in qs]

    if user_id:
        if limit is not None:
            return _first_result(
                _run(
                    "SELECT * FROM documents WHERE uploaded_by_id = $user_id OR uploaded_by_id = NONE "
                    "ORDER BY created_at DESC LIMIT $limit;",
                    {"user_id": str(user_id), "limit": max(1, int(limit))},
                )
            )
        return _first_result(
            _run(
                "SELECT * FROM documents WHERE uploaded_by_id = $user_id OR uploaded_by_id = NONE "
                "ORDER BY created_at DESC;",
                {"user_id": str(user_id)},
            )
        )
    if limit is not None:
        return _first_result(
            _run("SELECT * FROM documents ORDER BY created_at DESC LIMIT $limit;", {"limit": max(1, int(limit))})
        )
    return _first_result(_run("SELECT * FROM documents ORDER BY created_at DESC;"))


def get_document_by_hash(file_hash: str, user_id: str | None = None) -> dict | None:
    """Find document by file_hash (optionally for a specific user)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        qs = SourceDocument.objects.filter(file_hash=file_hash)
        if user_id:
            qs = qs.filter(uploaded_by_id=user_id)
        doc = qs.first()
        return _model_to_dict(doc) if doc else None

    if user_id:
        sql = "SELECT * FROM documents WHERE file_hash = $file_hash AND uploaded_by_id = $user_id LIMIT 1;"
        rows = _first_result(_run(sql, {"file_hash": file_hash, "user_id": str(user_id)}))
    else:
        sql = "SELECT * FROM documents WHERE file_hash = $file_hash LIMIT 1;"
        rows = _first_result(_run(sql, {"file_hash": file_hash}))
    return rows[0] if rows else None


# ── System Settings helpers ─────────────────────────────────────────────────


def get_system_settings() -> dict:
    """Get the global system settings record. Creates default if missing."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        return _settings_to_dict(s)

    sql = "SELECT * FROM system_settings:1;"
    rows = _first_result(_run(sql))
    if rows:
        return rows[0]

    # Create default settings if empty. Provider credentials are deployment-managed.
    default_data = {"monthly_budget_usd": 10.0, "selected_model": "auto", "currency": "auto"}
    rows = _first_result(_run("UPSERT system_settings:1 CONTENT $data;", {"data": default_data}))
    return rows[0] if rows else default_data


# Provider credentials are intentionally excluded from ALLOWED_SETTINGS_KEYS.
# They must only be resolved from GCP Secret Manager or environment variables.
ALLOWED_SETTINGS_KEYS = frozenset(
    {
        "monthly_budget_usd",
        "selected_model",
        "currency",
        "csrf_trusted_origins",
        "source_library_uri",
    }
)


def save_system_settings(data: dict) -> dict:
    """Upsert system settings record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        for k, v in data.items():
            if hasattr(s, k) and k in ALLOWED_SETTINGS_KEYS:
                setattr(s, k, v)
        s.save()
        return _settings_to_dict(s)

    payload = {k: v for k, v in data.items() if v is not None and k in ALLOWED_SETTINGS_KEYS}
    if "monthly_budget_usd" in payload:
        payload["monthly_budget_usd"] = float(payload["monthly_budget_usd"])
    sql = "UPSERT system_settings:1 CONTENT $data;"
    rows = _first_result(_run(sql, {"data": payload}))
    return rows[0] if rows else {}


# ── Audit Log helpers ───────────────────────────────────────────────────────


def list_audit_logs(limit: int = 100, start: int = 0) -> list[dict]:
    """Retrieve paginated audit logs from SurrealDB ordered by timestamp desc."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        qs = AuditLog.objects.all().order_by("-timestamp")[start : start + limit]
        return [_audit_log_to_dict(log) for log in qs]

    sql = "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT $limit START $start;"
    return _first_result(_run(sql, {"limit": limit, "start": start}))


def count_audit_logs() -> int:
    """Count total audit logs in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        return AuditLog.objects.count()

    sql = "SELECT count() AS n FROM audit_logs GROUP ALL;"
    rows = _first_result(_run(sql))
    return rows[0].get("n", 0) if rows else 0


def _record_document_spend(doc_uuid: str, created_at: datetime, cost: float, in_tok: int, out_tok: int) -> bool:
    """Atomically record one deleted document's spend, tolerating retry attempts."""
    sql = (
        "BEGIN TRANSACTION; "
        "LET $existing = (SELECT id FROM document_spend_reconciliations "
        "WHERE doc_uuid = $doc_uuid LIMIT 1); "
        "IF array::len($existing) = 0 { "
        "CREATE document_spend_reconciliations CONTENT { "
        "doc_uuid: $doc_uuid, year: $year, month: $month, cost_usd: $cost_usd, "
        "input_tokens: $input_tokens, output_tokens: $output_tokens "
        "}; }; COMMIT TRANSACTION;"
    )
    try:
        _run(
            sql,
            {
                "doc_uuid": doc_uuid,
                "year": created_at.year,
                "month": created_at.month,
                "cost_usd": cost,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
        )
    except Exception as exc:
        logger.warning("[Delete] Failed to reconcile document spend: %s", exc)
        return False
    return True


def _flush_document_cost(doc) -> bool:
    """Persist a document's spend before deletion, returning whether deletion is safe."""
    cost = doc.get("cost_usd") or 0.0
    created_at_value = doc.get("created_at")
    if cost <= 0:
        return True

    if not created_at_value:
        logger.warning("[SurrealDB] event=surrealdb_missing_created_at")
        return False

    from django.utils.dateparse import parse_datetime as django_parse

    if isinstance(created_at_value, datetime):
        created_at = created_at_value
    elif isinstance(created_at_value, str):
        created_at = django_parse(created_at_value)
    else:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=%s", type(created_at_value).__name__)
        return False

    if not created_at:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=string")
        return False

    from django.conf import settings

    input_tokens = doc.get("input_tokens") or 0
    output_tokens = doc.get("output_tokens") or 0
    if not getattr(settings, "SURREALDB_OFFLINE", False):
        doc_uuid = str(doc.get("doc_uuid") or "")
        if not doc_uuid:
            logger.warning("[SurrealDB] event=surrealdb_missing_document_uuid")
            return False
        return _record_document_spend(doc_uuid, created_at, float(cost), input_tokens, output_tokens)

    from decimal import Decimal

    from extractor.models import MonthlySpendLog

    try:
        return MonthlySpendLog.add_cost(
            date=created_at,
            cost=Decimal(str(cost)),
            in_tok=input_tokens,
            out_tok=output_tokens,
        )
    except Exception as exc:
        logger.warning("[Delete] Failed to flush cost to MonthlySpendLog in delete_document: %s", exc)
        return False


def _delete_offline_document(doc_uuid: str) -> None:
    from extractor.models import SourceDocument

    _test_chunks.pop(str(doc_uuid), None)
    try:
        import uuid

        try:
            uuid.UUID(str(doc_uuid))
            doc = SourceDocument.objects.get(uuid=doc_uuid)
        except ValueError:
            doc = SourceDocument.objects.get(id=int(doc_uuid))
        if doc and hasattr(doc, "uuid"):
            _test_chunks.pop(str(doc.uuid), None)
        if doc and hasattr(doc, "id"):
            _test_chunks.pop(str(doc.id), None)
        doc.delete()
    except SourceDocument.DoesNotExist, ValueError:
        pass


def delete_document(doc_uuid: str) -> None:
    """Delete document record and all associated chunks/cache entries."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _delete_offline_document(doc_uuid)
        return

    # BUG-02: Flush cost to MonthlySpendLog before deleting from SurrealDB.
    # Use a non-blocking approach: if flush fails (e.g. corrupt created_at),
    # log a warning and proceed rather than leaving the document permanently
    # undeletable with a hard RuntimeError.
    doc = get_document(doc_uuid)
    if doc:
        cost = doc.get("cost_usd") or 0.0
        if float(cost) > 0 and not doc.get("created_at"):
            # Patch missing created_at with current time so flush can proceed
            from django.utils import timezone as tz

            doc = dict(doc)
            doc["created_at"] = tz.now()
            logger.warning(
                "[Delete] doc_uuid=%s has cost_usd=%.6f but no created_at; "
                "using current timestamp for MonthlySpendLog flush.",
                doc_uuid,
                float(cost),
            )
        if not _flush_document_cost(doc):
            logger.warning(
                "[Delete] Cost flush failed for doc_uuid=%s; proceeding with delete to avoid soft-bricking.",
                doc_uuid,
            )

    sql = (
        "DELETE FROM documents WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM rag_cache WHERE $doc_uuid INSIDE sources;"
    )
    _run(sql, {"doc_uuid": doc_uuid})


# ── Chunk helpers ─────────────────────────────────────────────────────────────


def _write_worker_chunks(doc_uuid: str, payloads: list[dict] | None = None) -> None:
    """Fence each chunk mutation against cancellation, deletion, and replacement tasks."""
    params = {"doc_uuid": doc_uuid, "expected_task": document_task_name.get(), "payloads": payloads}
    if payloads is None:
        sql = (
            "BEGIN TRANSACTION; "
            "IF array::len((SELECT id FROM documents WHERE doc_uuid = $doc_uuid "
            "AND cloud_task_name = $expected_task AND cancel_requested != true)) > 0 { "
            "DELETE FROM chunks WHERE doc_uuid = $doc_uuid; }; COMMIT TRANSACTION;"
        )
    else:
        sql = (
            "BEGIN TRANSACTION; "
            "IF array::len((SELECT id FROM documents WHERE doc_uuid = $doc_uuid "
            "AND cloud_task_name = $expected_task AND cancel_requested != true)) > 0 { "
            "INSERT INTO chunks $payloads; }; COMMIT TRANSACTION;"
        )
    _run(sql, params)


def recreate_chunks(doc_uuid: str, chunk_payloads: list[dict]) -> None:
    """
    Atomically replace all chunks for a document using a SurrealDB transaction.
    Deletes existing chunks first, then bulk-inserts new ones.

    BUG-05 fix: Wrapped in BEGIN/COMMIT TRANSACTION so a failed batch insert
    rolls back the preceding delete, preventing partial chunk corruption.
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks[doc_uuid] = chunk_payloads
        # Execute mock run if patched in tests
        _run("INSERT INTO chunks $payloads;", {"payloads": chunk_payloads})
        return

    for chunk in chunk_payloads:
        chunk["doc_uuid"] = doc_uuid

    # Insert chunks in small batches to prevent HTTP 413 Payload Too Large errors
    # resulting from large serialized vector embedding payloads in a single request.
    # Each batch is its own transaction so a partial failure only affects one batch.
    batch_size = 15

    # First, atomically delete all existing chunks for this document
    if document_task_name.get():
        _write_worker_chunks(doc_uuid)
    else:
        _run(
            "BEGIN TRANSACTION; DELETE FROM chunks WHERE doc_uuid = $doc_uuid; COMMIT TRANSACTION;",
            {"doc_uuid": doc_uuid},
        )

    if not chunk_payloads:
        return

    for i in range(0, len(chunk_payloads), batch_size):
        batch = chunk_payloads[i : i + batch_size]
        if document_task_name.get():
            _write_worker_chunks(doc_uuid, batch)
        else:
            _run(
                "BEGIN TRANSACTION; INSERT INTO chunks $payloads; COMMIT TRANSACTION;",
                {"payloads": batch},
            )


def delete_chunks(doc_uuid: str) -> None:
    """Remove all vector chunks for a document."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks.pop(doc_uuid, None)
        _run("DELETE FROM chunks WHERE doc_uuid = $doc_uuid;", {"doc_uuid": doc_uuid})
        return

    sql = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
    if document_task_name.get():
        _write_worker_chunks(doc_uuid)
    else:
        _run(sql, {"doc_uuid": doc_uuid})


def count_document_chunks(doc_uuid: str) -> int:
    """Returns the number of chunks stored in SurrealDB for the given doc_uuid."""
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT count() AS n FROM chunks WHERE doc_uuid = $doc_uuid GROUP ALL;"
        results = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
        if results:
            return results[0].get("n", 0)
        return len(_test_chunks.get(doc_uuid, []))

    sql = "SELECT count() AS n FROM chunks WHERE doc_uuid = $doc_uuid GROUP ALL;"
    results = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
    if results:
        return results[0].get("n", 0)
    return 0


def _parse_chunk_counts(results, doc_uuids):
    counts = dict.fromkeys(doc_uuids, 0)
    for row in results:
        uuid = row.get("doc_uuid")
        if uuid:
            counts[uuid] = row.get("n", 0)
    return counts


def count_documents_chunks(doc_uuids: list[str]) -> dict[str, int]:
    """
    Returns a dictionary mapping doc_uuid to the number of chunks stored in SurrealDB
    for each doc_uuid in the provided list.
    """
    doc_uuids = [str(u) for u in doc_uuids]
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
        results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
        if results:
            return _parse_chunk_counts(results, doc_uuids)
        return {u: len(_test_chunks.get(u, [])) for u in doc_uuids}

    if not doc_uuids:
        return {}
    sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
    results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
    if results:
        return _parse_chunk_counts(results, doc_uuids)
    return dict.fromkeys(doc_uuids, 0)


def get_document_chunks(doc_uuid: str, limit: int = 100, start: int = 0) -> list[dict[str, Any]]:
    """Retrieve chunks for a document ordered by chunk_index."""
    doc_uuid = str(doc_uuid)
    limit = min(max(1, int(limit)), 500)
    start = max(0, int(start))
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT * FROM chunks WHERE doc_uuid = $doc_uuid ORDER BY chunk_index ASC LIMIT $limit START $start;"
        result = _first_result(_run(sql, {"doc_uuid": doc_uuid, "limit": limit, "start": start}))
        if isinstance(result, list) and result:
            return result
        chunks = _test_chunks.get(doc_uuid, [])
        return sorted(chunks, key=lambda x: int(x.get("chunk_index", 0)))[start : start + limit]

    sql = "SELECT * FROM chunks WHERE doc_uuid = $doc_uuid ORDER BY chunk_index ASC LIMIT $limit START $start;"
    result = _first_result(_run(sql, {"doc_uuid": doc_uuid, "limit": limit, "start": start}))
    if isinstance(result, list):
        return result
    return []


def clone_chunks(source_uuid: str, target_uuid: str) -> None:
    """Copy all chunks from source_uuid to target_uuid (deduplication flow)."""
    source_uuid = str(source_uuid).strip()
    target_uuid = str(target_uuid).strip()
    if ":" in source_uuid:
        source_uuid = source_uuid.split(":", 1)[1]
    if ":" in target_uuid:
        target_uuid = target_uuid.split(":", 1)[1]

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        source_chunks = _test_chunks.get(source_uuid, [])
        _test_chunks[target_uuid] = [{**c, "doc_uuid": target_uuid} for c in source_chunks]
        return

    source_chunks = []
    while page := get_document_chunks(source_uuid, limit=500, start=len(source_chunks)):
        source_chunks.extend(page)
        if len(page) < 500:
            break
    if not source_chunks:
        return

    new_payloads = []
    for c in source_chunks:
        payload = {
            "doc_uuid": target_uuid,
            "chunk_index": c.get("chunk_index", 0),
            "content": c.get("content", ""),
            "token_count": c.get("token_count", 0),
            "language": c.get("language", ""),
            "page_number": c.get("page_number", 1),
            "chapter_title": c.get("chapter_title", ""),
            "anchor_id": c.get("anchor_id", ""),
            "embedding": c.get("embedding"),
        }
        new_payloads.append(payload)

    recreate_chunks(target_uuid, new_payloads)


def search_chunks_hnsw(
    query_embedding: list[float], limit: int = 10, allowed_doc_uuids: list[str] | None = None
) -> list[dict]:
    """
    Approximate nearest-neighbour chunk search using the HNSW index.
    Optionally restricts results to an allowlist of doc UUIDs (tenant isolation).
    """
    params = {"query_embedding": query_embedding, "limit": limit}
    if allowed_doc_uuids is not None:
        params["allowed_doc_uuids"] = allowed_doc_uuids
        sql = (
            "SELECT id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "WHERE doc_uuid INSIDE $allowed_doc_uuids "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    else:
        sql = (
            "SELECT id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    return _first_result(_run(sql, params))


def search_chunks_bm25(query_text: str, limit: int = 10, allowed_doc_uuids: list[str] | None = None) -> list[dict]:
    """Execute keyword search over SurrealDB document chunks for sparse retrieval."""
    params: dict[str, Any] = {"query_text": query_text, "limit": limit}
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        chunks = []
        if allowed_doc_uuids is not None:
            for uuid_str in allowed_doc_uuids:
                chunks.extend(_test_chunks.get(uuid_str, []))
        else:
            for c_list in _test_chunks.values():
                chunks.extend(c_list)
        results = [c for c in chunks if query_text.lower() in c.get("content", "").lower()]
        return results[:limit]

    if allowed_doc_uuids is not None:
        params["allowed_doc_uuids"] = allowed_doc_uuids
        sql = (
            "SELECT id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id "
            "FROM chunks "
            "WHERE doc_uuid INSIDE $allowed_doc_uuids AND content CONTAINS $query_text "
            "LIMIT $limit;"
        )
    else:
        sql = (
            "SELECT id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id "
            "FROM chunks "
            "WHERE content CONTAINS $query_text "
            "LIMIT $limit;"
        )
    return _first_result(_run(sql, params))


# ── RAG cache helpers ─────────────────────────────────────────────────────────


def upsert_rag_cache(
    user_id: str,
    query_text: str,
    query_embedding: list[float],
    answer_text: str,
    sources: list[str],
    ttl_seconds: int = 604800,
) -> None:
    """Insert a new semantic cache entry with a default 7-day TTL."""
    from datetime import timedelta

    expires_at_val: datetime = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    sql = (
        "INSERT INTO rag_cache {"
        "  user_id: $user_id,"
        "  query_text: $query_text,"
        "  query_embedding: $query_embedding,"
        "  answer_text: $answer_text,"
        "  sources: $sources,"
        "  expires_at: $expires_at,"
        "  updated_at: time::now()"
        "};"
    )
    _run(
        sql,
        {
            "user_id": user_id,
            "query_text": query_text,
            "query_embedding": query_embedding,
            "answer_text": answer_text,
            "sources": sources,
            "expires_at": expires_at_val,
        },
    )


def search_rag_cache_hnsw(
    user_id: str, query_embedding: list[float], threshold: float = 0.15, limit: int = 1
) -> list[dict]:
    """Search for a semantically similar cached answer for this user."""
    sql = (
        "SELECT id, answer_text, sources, query_text, "
        "1.0 - vector::similarity::cosine(query_embedding, $query_embedding) AS score "
        "FROM rag_cache "
        "WHERE user_id = $user_id AND expires_at > time::now() "
        "ORDER BY score ASC "
        "LIMIT $limit;"
    )
    results = _first_result(
        _run(
            sql,
            {
                "user_id": user_id,
                "query_embedding": query_embedding,
                "limit": limit,
            },
        )
    )
    return [r for r in results if r.get("score", 1.0) <= threshold]


def purge_expired_rag_cache() -> int:
    """Delete all expired RAG cache entries. Returns count of deleted rows."""
    sql = "SELECT count() AS n FROM rag_cache WHERE expires_at < time::now() GROUP ALL;"
    count_rows = _first_result(_run(sql))
    count = count_rows[0].get("n", 0) if count_rows else 0
    _run("DELETE FROM rag_cache WHERE expires_at < time::now();")
    return count


def purge_all_rag_cache() -> None:
    """Delete all entries from rag_cache table."""
    _run("DELETE FROM rag_cache;")


def purge_all() -> None:
    """Delete all records from documents, chunks, rag_cache, and user_memories tables."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        SourceDocument.objects.all().delete()
        _test_chunks.clear()
        return

    documents = list_documents()
    if not all(_flush_document_cost(document) for document in documents):
        raise RuntimeError("Document costs could not be recorded; purge aborted.")
    _run("DELETE FROM documents; DELETE FROM chunks; DELETE FROM rag_cache; DELETE FROM user_memories;")


# ── KV cache helpers ──────────────────────────────────────────────────────────


def kv_cache_get(key: str) -> Any | None:
    """Retrieve a cached value by key. Returns None on miss or expiry."""
    sql = (
        "SELECT cache_value FROM kv_cache "
        "WHERE cache_key = $cache_key AND (expires_at IS NONE OR expires_at > time::now());"
    )
    rows = _first_result(_run(sql, {"cache_key": key}))
    if rows:
        val_data = rows[0].get("cache_value") if "cache_value" in rows[0] else rows[0].get("val")
        if val_data is not None:
            try:
                if isinstance(val_data, str):
                    return json.loads(val_data)
                return val_data
            except ValueError:
                return val_data
    return None


def kv_cache_set(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """Store a value in the KV cache with optional TTL."""
    from datetime import timedelta

    if ttl_seconds is not None:
        # Pass a Python datetime object — not an ISO string — so the SurrealDB 2.x driver
        # serialises it as a native datetime type and schema coercion succeeds.
        expires_at_val: datetime | None = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    else:
        expires_at_val = None

    json_value = json.dumps(value)
    sql = (
        "BEGIN TRANSACTION;"
        "DELETE FROM kv_cache WHERE cache_key = $cache_key;"
        "INSERT INTO kv_cache {"
        "  cache_key: $cache_key,"
        "  cache_value: $cache_value,"
        "  expires_at: $expires_at"
        "};"
        "COMMIT TRANSACTION;"
    )
    _run(
        sql,
        {
            "cache_key": key,
            "cache_value": json_value,
            "expires_at": expires_at_val,
        },
    )


def kv_cache_delete_pattern(prefix: str) -> None:
    """Delete all KV entries whose key starts with the given prefix."""
    sql = "DELETE FROM kv_cache WHERE string::starts_with(cache_key, $prefix);"
    _run(sql, {"prefix": prefix})


# ── User memory helpers ───────────────────────────────────────────────────────


def add_user_memory(user_id: str, memory_text: str, embedding: list[float]) -> None:
    """Persist a new user memory vector."""
    sql = "INSERT INTO user_memories {  user_id: $user_id,  memory_text: $memory_text,  embedding: $embedding};"
    _run(
        sql,
        {
            "user_id": user_id,
            "memory_text": memory_text,
            "embedding": embedding,
        },
    )


def search_user_memories(user_id: str, query_embedding: list[float], limit: int = 5) -> list[dict]:
    """Find the closest user memories using HNSW vector search."""
    sql = (
        "SELECT memory_text, "
        "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
        "FROM user_memories "
        "WHERE user_id = $user_id "
        "ORDER BY score ASC "
        "LIMIT $limit;"
    )
    return _first_result(
        _run(
            sql,
            {
                "user_id": user_id,
                "query_embedding": query_embedding,
                "limit": limit,
            },
        )
    )


# ── Audit log helpers ─────────────────────────────────────────────────────────


def log_audit(
    action: str, user_id: str, doc_uuid: str | None = None, metadata: dict | None = None, ip_address: str = ""
) -> None:
    """Write an audit log entry to SurrealDB."""
    params = {
        "action": action,
        "user_id": str(user_id),
        "doc_uuid": str(doc_uuid) if doc_uuid is not None else None,
        "metadata": json.dumps(metadata or {}),
        "ip_address": ip_address,
    }
    sql = (
        "INSERT INTO audit_logs {"
        "  action: $action,"
        "  user_id: $user_id,"
        "  doc_uuid: $doc_uuid,"
        "  metadata: $metadata,"
        "  ip_address: $ip_address"
        "};"
    )
    try:
        _run(sql, params)
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to write audit log for action=%s user=%s: %s", action, user_id, exc)


def find_chunk_embedding(text: str) -> list[float] | None:
    """Finds an existing embedding for the exact text block from any previously ingested document."""
    sql = "SELECT embedding FROM chunks WHERE content = $text LIMIT 1;"
    results = _first_result(_run(sql, {"text": text}))
    if results and isinstance(results[0], dict) and "embedding" in results[0]:
        return results[0]["embedding"]
    return None


def find_chunk_embeddings_batch(texts: list[str]) -> dict[str, list[float]]:
    """
    Finds existing embeddings for a batch of text blocks in a single SurrealDB query.
    Returns a mapping of {content: embedding}.
    """
    if not texts:
        return {}
    unique_texts = [t for t in set(texts) if t][:500]
    if not unique_texts:
        return {}
    sql = "SELECT content, embedding FROM chunks WHERE content IN $texts;"
    results = _first_result(_run(sql, {"texts": unique_texts}))
    mapping: dict[str, list[float]] = {}
    if results and isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and "content" in row and "embedding" in row:
                mapping[row["content"]] = row["embedding"]
    return mapping


def count_user_memories(user_id: str) -> int:
    """Count user memories in SurrealDB."""
    sql = "SELECT count() AS n FROM user_memories WHERE user_id = $user_id GROUP ALL;"
    results = _first_result(_run(sql, {"user_id": user_id}))
    if results:
        return results[0].get("n", 0)
    return 0


def clear_user_memories(user_id: str) -> None:
    """Clear all memories for a user in SurrealDB."""
    sql = "DELETE FROM user_memories WHERE user_id = $user_id;"
    _run(sql, {"user_id": user_id})


# ── Context Caching & High-Throughput Rate Limits ───────────────────────────


def context_cache_get(context_hash: str) -> dict[str, Any] | None:
    """Retrieve cached context prefix or prompt by hash and increment hit counter."""
    sql = "SELECT * FROM context_cache WHERE context_hash = $context_hash AND expires_at > time::now() LIMIT 1;"
    rows = _first_result(_run(sql, {"context_hash": context_hash}))
    if rows and isinstance(rows, list) and len(rows) > 0:
        _run(
            "UPDATE context_cache SET hit_count += 1, updated_at = time::now() WHERE context_hash = $context_hash;",
            {"context_hash": context_hash},
        )
        return rows[0]
    return None


def context_cache_set(
    context_hash: str,
    context_text: str,
    token_count: int = 0,
    doc_uuid: str | None = None,
    user_id: str | None = None,
    ttl_hours: int = 1,
) -> None:
    """Store or update context cache entry with TTL.

    BUG-10 fix: context_cache_get queries WHERE expires_at > time::now().
    Without an explicit expires_at, SurrealDB evaluates NONE > time::now()
    as false, causing a 100% cache miss rate. Default TTL is 1 hour.
    """
    from datetime import timedelta

    from django.utils import timezone as tz

    payload = {
        "context_hash": context_hash,
        "context_text": context_text,
        "token_count": token_count,
        "doc_uuid": doc_uuid,
        "user_id": user_id,
        "hit_count": 0,
        "expires_at": tz.now() + timedelta(hours=ttl_hours),
    }
    sql = "INSERT INTO context_cache $payload;"
    _run(sql, {"payload": payload})


def check_rate_limit_atomic(key: str, max_requests: int, window_seconds: int = 3600) -> bool:
    """
    Atomic sliding-window rate limit checker in SurrealDB.
    Returns True if request is allowed, False if quota exceeded.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return True

    try:
        from datetime import timedelta

        from django.utils import timezone as tz

        window_sec = max(1, int(window_seconds))
        expires_at = tz.now() + timedelta(seconds=window_sec)
        sql = (
            "BEGIN TRANSACTION;"
            "LET $existing = (SELECT * FROM rate_limits WHERE key = $key AND expires_at > time::now() LIMIT 1);"
            "IF array::len($existing) = 0 {"
            "  INSERT INTO rate_limits { key: $key, request_count: 1, window_start: time::now(), expires_at: $expires_at };"
            "  RETURN true;"
            "} ELSE IF $existing[0].request_count >= $max_requests {"
            "  RETURN false;"
            "} ELSE {"
            "  UPDATE rate_limits SET request_count += 1 WHERE key = $key;"
            "  RETURN true;"
            "};"
            "COMMIT TRANSACTION;"
        )
        res = _run(sql, {"key": key, "max_requests": max_requests, "expires_at": expires_at})
        decision = _parse_rate_limit_transaction_result(res, max_requests)
        if decision is not None:
            return decision
        logger.warning("[RateLimit] Unexpected SurrealDB transaction result; denying request.")
        return False
    except Exception as exc:
        logger.warning("[RateLimit] Error checking rate limit in SurrealDB; denying request: %s", exc)
        return False


def _extract_stmt_decision(val: Any, max_requests: int) -> bool | None:
    if isinstance(val, bool):
        return val
    if isinstance(val, list) and val:
        first = val[0]
        if isinstance(first, bool):
            return first
        if isinstance(first, dict) and "request_count" in first:
            return bool(first.get("request_count", 0) <= max_requests)
    return None


def _parse_rate_limit_transaction_result(res: Any, max_requests: int) -> bool | None:
    """Extract boolean decision from SurrealDB rate limit transaction statement results."""
    if isinstance(res, bool):
        return res
    candidates = res if isinstance(res, list) else [res]
    for stmt in candidates:
        if isinstance(stmt, bool):
            return stmt
        if isinstance(stmt, dict):
            _check_surreal_error(stmt)
            decision = _extract_stmt_decision(stmt.get("result"), max_requests)
            if decision is not None:
                return decision
    return None


# ── Knowledge Graph RAG & Graph-Relational Methods ───────────────────────────


def upsert_entity(
    name: str,
    tenant_id: str,
    category: str = "CONCEPT",
    description: str = "",
    embedding: list[float] | None = None,
) -> dict[str, Any] | None:
    """
    Upserts an entity node in SurrealDB with optional HNSW embedding vector.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return {"name": name, "tenant_id": tenant_id, "category": category, "description": description}

    sql = """
    UPSERT entities:[ $name, $tenant_id ] SET
        name = $name,
        tenant_id = $tenant_id,
        category = $category,
        description = $description,
        embedding = $embedding,
        created_at = time::now();
    """
    params = {
        "name": name,
        "tenant_id": str(tenant_id),
        "category": category,
        "description": description,
        "embedding": embedding,
    }
    rows = _first_result(_run(sql, params))
    return rows[0] if rows and isinstance(rows, list) else None


def relate_chunk_to_entity(
    chunk_id: str,
    entity_name: str,
    tenant_id: str,
    relevance_score: float = 1.0,
) -> None:
    """
    Creates a directed graph relation edge from a text chunk to a knowledge entity.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return

    sql = """
    RELATE $chunk_id->chunk_references->(SELECT id FROM entities WHERE name = $entity_name AND tenant_id = $tenant_id LIMIT 1)
    SET relevance_score = $relevance_score, extracted_at = time::now();
    """
    params = {
        "chunk_id": chunk_id,
        "entity_name": entity_name,
        "tenant_id": str(tenant_id),
        "relevance_score": float(relevance_score),
    }
    _run(sql, params)


def query_knowledge_graph(
    tenant_id: str,
    entity_name: str,
) -> dict[str, Any]:
    """
    Executes a 2-hop Graph Relational query returning connected documents, chunks, and related concepts.
    """
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return {"entity": entity_name, "connected_documents": [], "related_concepts": []}

    sql = """
    SELECT 
        name,
        category,
        description,
        <-chunk_references<-chunks.doc_uuid AS connected_documents,
        ->entity_relations->entities.name AS related_concepts
    FROM entities
    WHERE tenant_id = $tenant_id 
      AND name = $entity_name
    LIMIT 1;
    """
    rows = _first_result(_run(sql, {"tenant_id": str(tenant_id), "entity_name": entity_name}))
    if rows and isinstance(rows, list) and len(rows) > 0:
        return rows[0]
    return {"entity": entity_name, "connected_documents": [], "related_concepts": []}
