"""
KORDA SurrealDB Knowledge Engine Adapter

Implements an asynchronous SurrealDB client adapter powered by AsyncSurreal
and async-to-sync boundaries for thread safety and high-throughput execution.

Capabilities:
  - Document metadata storage & transactional CRUD
  - Vector embeddings & chunk storage with HNSW 768 cosine similarity index
  - Distributed atomic sliding-window rate limiting (`rate_limits`)
  - Semantic user memories (`user_memories`)
  - Offline fallback integration for unit testing and local development
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import logging
import os
import re
import sys
import threading
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from surrealdb import AsyncSurreal

from extractor.task_state import CANCEL_REQUESTED, CLOUD_TASK_NAME, PENDING_OPERATION

logger = logging.getLogger(__name__)
document_task_name: ContextVar[str] = ContextVar("document_task_name", default="")

# Sentinel for update_document: pass CLEAR_FIELD as a value to set the field
# to NULL/NONE. Plain None still means "leave unchanged" (AUD-324).
CLEAR_FIELD = object()

# Canonical allowlist of writable documents fields; must match schema.surql.
# create_document and _update_document_surreal both filter payloads through it.
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
    PENDING_OPERATION,
}


# ── Singleton connection pool ──────────────────────────────────────────────────
# A single httpx.Client is shared across all threads (safe per httpx docs).
# It maintains an internal connection pool, reusing TCP sockets on Keep-Alive.


_test_chunks: dict[str, list[dict]] = {}
_test_korda_sources: dict[str, dict[str, Any]] = {}
_test_korda_anchors: dict[str, dict[str, Any]] = {}
_test_korda_crystals: dict[str, dict[str, Any]] = {}
_test_korda_exam_items: dict[str, dict[str, Any]] = {}
_test_korda_attempts: list[dict[str, Any]] = []


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

    local_ws_scheme = "ws:" + "//"
    ws_schemes = (local_ws_scheme, "wss:" + "//")

    if url.startswith("https://"):
        url = "wss://" + url.removeprefix("https://")
    elif url.startswith("http://"):
        url = local_ws_scheme + url.removeprefix("http://")
    if not url.startswith(ws_schemes):
        raise ValueError("SURREAL_URL must use a ws or wss scheme.")
    if not getattr(settings, "DEBUG", True) and url.startswith(local_ws_scheme):
        raise ValueError("Production SURREAL_URL must use encrypted wss transport.")
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


async def _perform_ns_detection(db, db_name: str, fallback_ns: str) -> str:
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
            # Namespace probing issues USE per candidate on the connection. It
            # must run on a dedicated probe session so it cannot mutate the
            # shared connection's session namespace while concurrent queries
            # are in flight.
            async with AsyncSurreal(url) as probe_db:
                await probe_db.signin(auth)
                _detected_ns = await _perform_ns_detection(probe_db, db_name, fallback_ns)
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


# ── Dedicated RPC event loop & shared connection ──────────────────────────────
# A single long-lived WebSocket connection is owned by a dedicated daemon
# thread running its own event loop. Every synchronous caller submits coroutines
# via run_coroutine_threadsafe, so queries multiplex over ONE TLS session
# instead of paying a full TCP+TLS+WebSocket handshake per query. This removes
# the handshake storm that triggered intermittent TLS alerts against managed
# SurrealDB endpoints and cuts per-query latency dramatically.

_RPC_LOOP: asyncio.AbstractEventLoop | None = None
_RPC_LOOP_THREAD: threading.Thread | None = None
_RPC_LOOP_GUARD = threading.Lock()
_RPC_DB: Any = None
_RPC_DB_NS: str | None = None
_RPC_DB_NAME: str | None = None
_RPC_DB_LOCK: asyncio.Lock | None = None
_RPC_DB_LOCK_GUARD = threading.Lock()
_RPC_ADMISSION: threading.BoundedSemaphore | None = None


def _surreal_executor_workers() -> int:
    """Return a bounded RPC executor size suitable for the current deployment."""
    from django.conf import settings

    return max(1, getattr(settings, "SURREAL_EXECUTOR_WORKERS", 16))


def _ensure_rpc_loop() -> asyncio.AbstractEventLoop:
    """Lazily start the dedicated SurrealDB RPC event loop thread."""
    global _RPC_LOOP, _RPC_LOOP_THREAD
    loop = _RPC_LOOP
    if loop is not None and loop.is_running():
        return loop
    with _RPC_LOOP_GUARD:
        if _RPC_LOOP is not None and _RPC_LOOP.is_running():
            return _RPC_LOOP
        new_loop = asyncio.new_event_loop()
        ready = threading.Event()

        def _loop_main() -> None:
            asyncio.set_event_loop(new_loop)
            ready.set()
            new_loop.run_forever()

        thread = threading.Thread(target=_loop_main, name="surreal-rpc-loop", daemon=True)
        thread.start()
        if not ready.wait(timeout=10):
            raise RuntimeError("SurrealDB RPC event loop failed to start.")
        _RPC_LOOP = new_loop
        _RPC_LOOP_THREAD = thread
        return new_loop


def _get_rpc_admission() -> threading.BoundedSemaphore:
    """Bound the number of in-flight RPC coroutines to preserve backpressure."""
    global _RPC_ADMISSION
    if _RPC_ADMISSION is None:
        with _RPC_LOOP_GUARD:
            if _RPC_ADMISSION is None:
                _RPC_ADMISSION = threading.BoundedSemaphore(_surreal_executor_workers() * 2)
    if _RPC_ADMISSION is None:
        raise RuntimeError("SurrealDB RPC admission control could not be initialized.")
    return _RPC_ADMISSION


def _rpc_db_lock() -> asyncio.Lock:
    """Return the loop-bound lock guarding shared connection setup."""
    global _RPC_DB_LOCK
    if _RPC_DB_LOCK is None:
        with _RPC_DB_LOCK_GUARD:
            if _RPC_DB_LOCK is None:
                _RPC_DB_LOCK = asyncio.Lock()
    if _RPC_DB_LOCK is None:
        raise RuntimeError("SurrealDB connection lock could not be initialized.")
    return _RPC_DB_LOCK


def _submit_to_rpc_loop(coro, timeout: float | None = None):
    """Schedule a coroutine on the dedicated RPC loop and wait for its result."""
    if threading.current_thread() is _RPC_LOOP_THREAD:
        coro.close()
        raise RuntimeError("SurrealDB RPC cannot be dispatched from the RPC loop thread.")
    loop = _ensure_rpc_loop()
    admission = _get_rpc_admission()
    if not admission.acquire(blocking=False):
        coro.close()
        raise RuntimeError("SurrealDB RPC executor is saturated; retry later.")
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    future.add_done_callback(lambda _f: admission.release())
    if timeout is None:
        timeout = max(1, getattr(settings, "SURREAL_RPC_TIMEOUT_SECONDS", 60))
    try:
        return future.result(timeout=max(1, timeout))
    except concurrent.futures.TimeoutError as exc:
        future.cancel()
        raise RuntimeError("SurrealDB RPC timed out; retry later.") from exc


def _connection_is_open(db) -> bool:
    """Best-effort liveness probe for the shared WebSocket connection."""
    socket = getattr(db, "socket", None)
    if socket is None:
        return False
    try:
        from websockets.protocol import State
    except Exception:
        return True
    state = getattr(socket, "state", None)
    # Non-State values come from test doubles; treat them as live and let the
    # query path trigger a reconnect if the connection is actually dead.
    if not isinstance(state, State):
        return True
    return state is State.OPEN


async def _drop_shared_db() -> None:
    """Close and discard the shared connection; safe to call on the RPC loop."""
    global _RPC_DB, _RPC_DB_NS, _RPC_DB_NAME
    db, _RPC_DB, _RPC_DB_NS, _RPC_DB_NAME = _RPC_DB, None, None, None
    if db is not None:
        try:
            await db.close()
        except Exception as close_err:
            logger.debug("[SurrealDB] Shared connection close failed: %s", close_err)


async def _ensure_shared_db(url: str, auth: dict):
    """Return a connected, authenticated shared connection (loop-confined)."""
    global _RPC_DB, _RPC_DB_NS, _RPC_DB_NAME
    async with _rpc_db_lock():
        if _RPC_DB is not None and _connection_is_open(_RPC_DB):
            return _RPC_DB
        await _drop_shared_db()
        db = AsyncSurreal(url)
        try:
            await db.connect(url)
            await db.signin(auth)
        except Exception:
            try:
                await db.close()
            except Exception as close_err:
                logger.debug("[SurrealDB] Failed connection cleanup after signin/connect error: %s", close_err)
            raise
        _RPC_DB = db
        _RPC_DB_NS = None
        _RPC_DB_NAME = None
        logger.info("[SurrealDB] Shared WebSocket connection established.")
        return db


async def _use_ns_db_if_needed(db, ns: str, db_name: str) -> None:
    """Issue USE only when the session namespace/database actually changes."""
    global _RPC_DB_NS, _RPC_DB_NAME
    if ns == _RPC_DB_NS and db_name == _RPC_DB_NAME:
        return
    await db.use(ns, db_name)
    _RPC_DB_NS = ns
    _RPC_DB_NAME = db_name


class _SurrealStatementError(RuntimeError):
    """Deterministic SurrealDB statement/protocol error; retrying cannot succeed."""


def _extract_statement_results(response: Any) -> list[dict]:
    if not isinstance(response, dict):
        return []
    rpc_error = response.get("error")
    if rpc_error is not None:
        raise _SurrealStatementError(f"SurrealDB RPC error: {rpc_error}")
    stmts = response.get("result")
    if not isinstance(stmts, list):
        return []
    return [s for s in stmts if isinstance(s, dict)]


async def _async_run(sql: str, params: dict | None = None) -> list[dict]:
    url = _get_surreal_url()
    auth = _get_surreal_auth()
    db_name = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))

    global _detected_ns
    # EDGE-04 fix: retry with exponential backoff to handle transient TCP resets,
    # SurrealDB container restarts, and Cloud Run cold-start connection delays.
    _max_attempts = 3
    last_exc: Exception | None = None
    for attempt in range(_max_attempts):
        try:
            if not _detected_ns and "INFO FOR ROOT" not in sql:
                # Detection probes namespaces on a dedicated connection so it
                # cannot mutate the shared session while queries are in flight.
                await _detect_active_namespace(url, auth, db_name)
            db = await _ensure_shared_db(url, auth)
            ns, _ = _get_surreal_ns_db()
            await _use_ns_db_if_needed(db, ns, db_name)
            # query_raw returns every per-statement result; db.query()
            # collapses to response["result"][0] and would hide later
            # statements (RETURN values, COMMIT, RELATE rows) — which
            # broke transaction callers that parse statement results.
            return _extract_statement_results(await db.query_raw(sql, params))
        except _SurrealStatementError:
            # Semantic/protocol errors are deterministic — retrying the same
            # statement cannot succeed and may double-apply ambiguous commits.
            raise
        except Exception as exc:
            last_exc = exc
            # Drop the shared connection so the next attempt re-establishes a
            # clean session (covers TCP resets, idle timeouts, and token expiry).
            await _drop_shared_db()
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


def _run(sql: str, params: dict | None = None) -> list[dict]:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [{"status": "OK", "result": []}]

    return _submit_to_rpc_loop(_async_run(sql, params))


def _check_surreal_error(obj: Any) -> None:
    if isinstance(obj, list):
        for item in obj:
            _check_surreal_error(item)
        return
    if isinstance(obj, dict) and obj.get("status") == "ERR":
        detail = obj.get("detail") or obj.get("information") or "Unknown SurrealDB error"
        raise RuntimeError(f"SurrealDB query failed: {detail}")


def _first_result(results: Any) -> list[Any]:
    if not results:
        return []
    if isinstance(results, list):
        _check_surreal_error(results)
        if len(results) > 0 and isinstance(results[0], dict) and "result" in results[0]:
            return results[0].get("result", [])
        return results
    if isinstance(results, dict):
        _check_surreal_error(results)
        if "result" in results:
            return results.get("result", [])
        return [results]
    return []


def _last_non_null_result(results: Any) -> Any:
    """Return the final non-None statement result from a multi-statement SurrealDB transaction."""
    if not results:
        return None
    if isinstance(results, dict):
        _check_surreal_error(results)
        return results.get("result") if "result" in results else results
    if not isinstance(results, list):
        return None
    _check_surreal_error(results)
    for item in reversed(results):
        val = item.get("result") if isinstance(item, dict) and "result" in item else item
        if val is not None:
            return val
    return None


async def _async_check_health() -> bool:
    url = _get_surreal_url()
    try:
        async with AsyncSurreal(url):
            logger.debug("[SurrealDB] WebSocket health check ping successful.")
        return True
    except Exception as e:
        logger.warning("[SurrealDB] WebSocket health check failed: %s", e)
        return False


async def _close_shared_db_on_loop() -> None:
    await _drop_shared_db()


def _shutdown_rpc_loop() -> None:
    """Best-effort orderly shutdown of the shared connection and RPC loop."""
    loop = _RPC_LOOP
    if loop is None or not loop.is_running():
        return
    try:
        asyncio.run_coroutine_threadsafe(_close_shared_db_on_loop(), loop).result(timeout=5)
    except Exception as shutdown_err:
        logger.debug("[SurrealDB] RPC loop shutdown warning: %s", shutdown_err)
    try:
        loop.call_soon_threadsafe(loop.stop)
    except Exception as stop_err:
        logger.debug("[SurrealDB] RPC loop stop warning: %s", stop_err)


atexit.register(_shutdown_rpc_loop)


def check_health() -> bool:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return False

    try:
        return bool(_submit_to_rpc_loop(_async_check_health(), timeout=15))
    except Exception as health_err:
        logger.warning("[SurrealDB] Health check dispatch failed: %s", health_err)
        return False


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
            except user_model.DoesNotExist, TypeError, ValueError:
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
        except user_model.DoesNotExist, TypeError, ValueError:
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


def _offline_document_lookup(doc_uuid):
    import uuid

    try:
        uuid.UUID(str(doc_uuid))
        return {"uuid": doc_uuid}
    except ValueError:
        return {"id": int(doc_uuid)}


def _retry_sqlite_lock(operation):
    """Retry only transient SQLite lock errors with a small bounded backoff."""
    import time

    from django.db import OperationalError, connection

    for attempt in range(3):
        try:
            return operation()
        except OperationalError as exc:
            if connection.vendor != "sqlite" or "locked" not in str(exc).lower() or attempt == 2:
                raise
            retry_number = attempt + 1
            time.sleep(0.05 * retry_number)
    raise RuntimeError("unreachable")


def _resolve_offline_owner_id(value, user_model):
    """Return value when it names an existing user PK, else None.

    Schema-less callers may pass non-numeric owner ids; an unguarded
    .filter(id=value) would raise ValueError and abort the whole update.
    """
    if not value:
        return None
    try:
        return value if user_model.objects.filter(id=value).exists() else None
    except TypeError, ValueError:
        return None


def _offline_update_values(data, user_model):
    from extractor.models import SourceDocument

    valid_fields = {field.name for field in SourceDocument._meta.fields}
    values = {}
    for key, value in data.items():
        if key not in valid_fields:
            continue
        if key == "uploaded_by_id":
            values[key] = _resolve_offline_owner_id(value, user_model)
        elif value is CLEAR_FIELD:
            values[key] = None
        elif key == "expires_at":
            from django.utils.dateparse import parse_datetime

            values[key] = parse_datetime(value) if value else None
        else:
            values[key] = value
    return values


def _update_document_worker_offline(source_document, lookup, data, user_model, expected_task):
    values = _offline_update_values(data, user_model)

    def conditional_update():
        from django.utils import timezone

        query = source_document.objects.filter(**lookup, cancel_requested=False)
        if expected_task:
            query = query.filter(cloud_task_name=expected_task)
        if not values:
            doc = query.first()
            return _model_to_dict(doc) if doc else {}
        if query.update(**values, updated_at=timezone.now()) != 1:
            return {}
        return _model_to_dict(source_document.objects.get(**lookup))

    return _retry_sqlite_lock(conditional_update)


def _update_document_offline(doc_uuid, data):
    from django.contrib.auth import get_user_model
    from django.db import transaction

    from extractor.models import SourceDocument

    user_model = get_user_model()
    try:
        lookup = _offline_document_lookup(doc_uuid)
    except ValueError:
        return {}

    expected_task = document_task_name.get()
    if CANCEL_REQUESTED not in data:
        return _update_document_worker_offline(SourceDocument, lookup, data, user_model, expected_task)

    with transaction.atomic():
        try:
            doc = SourceDocument.objects.select_for_update().get(**lookup)
        except SourceDocument.DoesNotExist:
            return {}

        _apply_offline_doc_update(doc, data, user_model)
        doc.save()
        return _model_to_dict(doc)


def _update_document_surreal(doc_uuid, data):
    # doc_uuid is the record identity used by the WHERE clause and $doc_uuid
    # param; letting payload rewrite it would silently re-key or no-op the
    # update. Identity changes are not a supported operation.
    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS and k != "doc_uuid"}
    if not payload:
        return get_document(doc_uuid) or {}

    set_parts = []
    params = {"doc_uuid": doc_uuid}
    for k, v in payload.items():
        _validate_field_name(k)
        if v is CLEAR_FIELD:
            set_parts.append(f"{k} = NONE")
        elif k in ("created_at", "updated_at", "expires_at"):
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


def touch_document(doc_uuid: str, expected_task: str = "") -> bool:
    """Refresh updated_at as a worker-liveness heartbeat for the stale-task reaper.

    When expected_task is given, the write is fenced to the owning Cloud Task
    identity so a superseded worker cannot keep a dead document looking alive.
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings
    from django.utils import timezone

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            lookup = _offline_document_lookup(doc_uuid)
        except TypeError, ValueError:
            return False
        qs = SourceDocument.objects.filter(**lookup)
        if expected_task:
            qs = qs.filter(cloud_task_name=expected_task, cancel_requested=False)
        return qs.update(updated_at=timezone.now()) == 1

    params: dict[str, Any] = {"doc_uuid": doc_uuid}
    condition = ""
    if expected_task:
        condition = " AND cloud_task_name = $expected_task AND cancel_requested != true"
        params["expected_task"] = expected_task
    sql = f"UPDATE documents SET updated_at = time::now() WHERE doc_uuid = $doc_uuid{condition};"  # nosec B608 # noqa: S608
    return bool(_first_result(_run(sql, params)))


def _conditional_update_document_offline(doc_uuid, data, allowed_statuses):
    """Atomic compare-and-swap update guarded by the document's current status."""
    from django.utils import timezone

    from extractor.models import SourceDocument

    try:
        lookup = _offline_document_lookup(doc_uuid)
    except TypeError, ValueError:
        return None

    payload = {
        k: (None if v is CLEAR_FIELD else v)
        for k, v in data.items()
        if k in {"status", "retry_count", "error_message", CANCEL_REQUESTED} and v is not None
    }
    if not payload:
        return None
    payload["updated_at"] = timezone.now()

    updated = SourceDocument.objects.filter(**lookup, status__in=list(allowed_statuses), cancel_requested=False).update(
        **payload
    )
    if updated != 1:
        return None
    try:
        return _model_to_dict(SourceDocument.objects.get(**lookup))
    except SourceDocument.DoesNotExist:
        return None


def _conditional_update_document_surreal(doc_uuid, data, allowed_statuses):
    """SurrealDB counterpart: UPDATE ... WHERE status still in the allowed set."""
    VALID_FIELDS = {"status", "retry_count", "error_message", CANCEL_REQUESTED}
    payload = {k: v for k, v in data.items() if k in VALID_FIELDS and v is not None}
    if not payload:
        return None

    set_parts = []
    params: dict[str, Any] = {"doc_uuid": doc_uuid, "allowed_statuses": list(allowed_statuses)}
    for k, v in payload.items():
        _validate_field_name(k)
        if v is CLEAR_FIELD:
            set_parts.append(f"{k} = NONE")
        else:
            set_parts.append(f"{k} = ${k}")
            params[k] = v
    set_parts.append("updated_at = time::now()")

    sql = (
        f"UPDATE documents SET {', '.join(set_parts)} WHERE doc_uuid = $doc_uuid "  # nosec B608 # noqa: S608
        "AND status IN $allowed_statuses AND cancel_requested != true RETURN AFTER;"
    )
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else None


def update_document_if_status(doc_uuid: str, data: dict, *, allowed_statuses: list[str]) -> dict | None:
    """Conditionally update a document only while its status remains in allowed_statuses.

    Compare-and-swap guard for the stale-task reaper: a worker that completes or
    fails the document between the scan snapshot and this write makes the UPDATE
    match zero rows, so the reaper cannot overwrite a fresher terminal state.
    Restricted to the reaper's narrow field set; views needing a full-payload
    CAS must use ``update_document_cas`` instead.
    Returns the updated row, or None when the guard rejected the write.
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _conditional_update_document_offline(doc_uuid, data, allowed_statuses)
    return _conditional_update_document_surreal(doc_uuid, data, allowed_statuses)


def _cas_update_document_offline(doc_uuid, data, allowed_statuses, allowed_pending_operations=None):
    """Offline counterpart of ``_cas_update_document_surreal``: single-statement
    UPDATE guarded by status so the check-and-write cannot interleave."""
    from django.contrib.auth import get_user_model
    from django.utils import timezone

    from extractor.models import SourceDocument

    try:
        lookup = _offline_document_lookup(doc_uuid)
    except TypeError, ValueError:
        return None

    values = _offline_update_values(data, get_user_model())
    if not values:
        return None
    values["updated_at"] = timezone.now()

    qs = SourceDocument.objects.filter(**lookup, status__in=list(allowed_statuses))
    if allowed_pending_operations is not None:
        qs = qs.filter(pending_operation__in=list(allowed_pending_operations))
    updated = qs.update(**values)
    if updated != 1:
        return None
    try:
        return _model_to_dict(SourceDocument.objects.get(**lookup))
    except SourceDocument.DoesNotExist:
        return None


def _cas_update_document_surreal(doc_uuid, data, allowed_statuses, allowed_pending_operations=None):
    """Full-payload CAS: same field handling as ``_update_document_surreal`` but
    the WHERE clause additionally requires status IN allowed_statuses. Worker
    task-name fencing is preserved when the ContextVar is set."""
    payload = {k: v for k, v in data.items() if v is not None and k in VALID_DOCUMENT_FIELDS and k != "doc_uuid"}
    if not payload:
        return get_document(doc_uuid) or {}

    set_parts = []
    params: dict[str, Any] = {"doc_uuid": doc_uuid, "allowed_statuses": list(allowed_statuses)}
    for k, v in payload.items():
        _validate_field_name(k)
        if v is CLEAR_FIELD:
            set_parts.append(f"{k} = NONE")
        elif k in ("created_at", "updated_at", "expires_at"):
            set_parts.append(f"{k} = <datetime> ${k}")
            params[k] = v
        else:
            set_parts.append(f"{k} = ${k}")
            params[k] = v

    if "updated_at" not in payload:
        set_parts.append("updated_at = time::now()")

    conditions = ["doc_uuid = $doc_uuid", "status IN $allowed_statuses"]
    if allowed_pending_operations is not None:
        conditions.append("pending_operation IN $allowed_pending_operations")
        params["allowed_pending_operations"] = list(allowed_pending_operations)
    if task_name := document_task_name.get():
        conditions.append("cloud_task_name = $expected_task AND cancel_requested != true")
        params["expected_task"] = task_name
    sql = f"UPDATE documents SET {', '.join(set_parts)} WHERE {' AND '.join(conditions)};"  # nosec B608 # noqa: S608
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else None


def update_document_cas(
    doc_uuid: str,
    data: dict,
    *,
    allowed_statuses: list[str],
    allowed_pending_operations: list[str] | None = None,
) -> dict | None:
    """Update a document only while its status remains in ``allowed_statuses``.

    General-purpose compare-and-swap for view-layer read-check-write flows:
    unlike ``update_document_if_status`` (reaper field whitelist) this accepts
    the full writable field set. Returns the updated row, or None when the
    status guard rejected the write (the document changed state concurrently).
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _cas_update_document_offline(doc_uuid, data, allowed_statuses, allowed_pending_operations)
    return _cas_update_document_surreal(doc_uuid, data, allowed_statuses, allowed_pending_operations)


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
                pending_docs = SourceDocument.objects.filter(id=int(doc_uuid), status="PENDING")
                document_id = pending_docs.values_list("id", flat=True).first()
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
        "UPDATE documents SET status = 'EXTRACTING', updated_at = time::now(), pending_operation = '' "
        "WHERE doc_uuid = $doc_uuid AND status = 'PENDING' AND cancel_requested != true RETURN AFTER;"
    )
    if task_name := document_task_name.get():
        params["expected_task"] = task_name
        sql = (
            "UPDATE documents SET status = 'EXTRACTING', updated_at = time::now(), pending_operation = '' "
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


def _split_offline_document_ids(clean_uuids: list[str]) -> tuple[list[int], list[str]]:
    """Split lookup keys into integer PKs and valid UUID strings.

    Invalid UUID strings make the ORM raise ValidationError for the whole
    IN clause — one bad entry must not drop the rest of the batch.
    """
    import uuid as _uuid

    int_ids: list[int] = []
    uuid_strs: list[str] = []
    for item in clean_uuids:
        try:
            int_ids.append(int(item))
            continue
        except ValueError, TypeError:
            pass
        try:
            _uuid.UUID(str(item))
        except ValueError, TypeError, AttributeError:
            continue
        uuid_strs.append(str(item))
    return int_ids, uuid_strs


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

        int_ids, uuid_strs = _split_offline_document_ids(clean_uuids)
        try:
            docs = SourceDocument.objects.filter(Q(id__in=int_ids) | Q(uuid__in=uuid_strs))
            return [_model_to_dict(doc) for doc in docs]
        except Exception as exc:
            logger.debug("[SurrealDB] Offline get_documents lookup failed: %s", exc)
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


# Fields rendered by the dashboard/status snapshot views. Excludes the heavy
# content blobs (raw_markdown, refined_markdown, yaml_metadata, qa_dataset) so
# polling endpoints do not ship megabytes per document per request.
_STATUS_DOC_FIELDS = (
    "id",
    "doc_uuid",
    "uploaded_by_id",
    "status",
    "error_message",
    "title",
    "original_filename",
    "author",
    "language",
    "document_type",
    "publisher",
    "publication_year",
    "license_type",
    "doi",
    "page_count",
    "cost_usd",
    "input_tokens",
    "output_tokens",
    "created_at",
    "updated_at",
)


def _offline_document_statuses(user_id: str | None, limit: int | None) -> list[dict]:
    from extractor.models import SourceDocument

    if user_id:
        from django.db.models import Q

        qs = SourceDocument.objects.filter(Q(uploaded_by_id=user_id) | Q(uploaded_by__isnull=True))
    else:
        qs = SourceDocument.objects.all()
    if limit is not None:
        qs = qs[: max(1, int(limit))]
    return [
        {
            "id": doc.id,
            "doc_uuid": str(doc.uuid),
            "uploaded_by_id": str(doc.uploaded_by_id) if doc.uploaded_by_id else None,
            "status": doc.status,
            "error_message": doc.error_message,
            "title": doc.title,
            "original_filename": doc.original_filename,
            "author": doc.author,
            "language": doc.language,
            "document_type": doc.document_type,
            "publisher": doc.publisher,
            "publication_year": doc.publication_year,
            "license_type": doc.license_type,
            "doi": doc.doi,
            "page_count": doc.page_count,
            "cost_usd": float(doc.cost_usd),
            "input_tokens": doc.input_tokens,
            "output_tokens": doc.output_tokens,
            "created_at": doc.created_at if doc.created_at else None,
            "updated_at": doc.updated_at if doc.updated_at else None,
        }
        for doc in qs
    ]


def list_document_statuses(user_id: str | None = None, limit: int | None = None) -> list[dict]:
    """Lightweight document listing for dashboard/status snapshots.

    Same tenant scoping and ordering as ``list_documents`` but projects only
    the fields the dashboard views render, avoiding full-content blobs.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _offline_document_statuses(user_id, limit)

    field_list = ", ".join(_STATUS_DOC_FIELDS)
    if user_id:
        scope = "WHERE uploaded_by_id = $user_id OR uploaded_by_id = NONE "
        params: dict[str, Any] = {"user_id": str(user_id)}
    else:
        scope = ""
        params = {}
    sql = f"SELECT {field_list} FROM documents {scope}ORDER BY created_at DESC"  # nosec B608 # noqa: S608 -- static field list and scope clause
    if limit is not None:
        sql += " LIMIT $limit;"
        params["limit"] = max(1, int(limit))
    else:
        sql += ";"
    return _first_result(_run(sql, params))


def get_user_total_spend(user_id: str | None = None, since: datetime | None = None):
    """Aggregate monthly/live document spend in SurrealDB without loading document blobs."""
    from decimal import Decimal

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.db.models import Sum

        from extractor.models import SourceDocument

        qs = SourceDocument.objects.all()
        if user_id:
            qs = qs.filter(uploaded_by_id=user_id)
        if since is not None:
            # Spend is attributed by last activity (updated_at), matching the
            # worker budget gate — created_at would miss month-boundary runs.
            qs = qs.filter(updated_at__gte=since)
        agg = qs.aggregate(total=Sum("cost_usd"))["total"]
        return Decimal(str(agg or 0))

    clauses: list[str] = []
    params: dict[str, Any] = {}
    if user_id:
        clauses.append("uploaded_by_id = $user_id")
        params["user_id"] = str(user_id)
    if since is not None:
        clauses.append("updated_at >= type::datetime($since)")
        params["since"] = since.isoformat()

    where_sql = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    sql = f"SELECT math::sum(cost_usd) AS total FROM documents{where_sql} GROUP ALL;"  # nosec B608 # noqa: S608
    rows = _first_result(_run(sql, params))
    if rows and isinstance(rows[0], dict):
        return Decimal(str(rows[0].get("total") or 0))
    return Decimal(0)


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


_SYSTEM_SETTINGS_CACHE_KEY = "surreal:system_settings"
_SYSTEM_SETTINGS_CACHE_TTL = 30


def get_system_settings() -> dict:
    """Get the global system settings record. Creates default if missing."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        return _settings_to_dict(s)

    # Settings change rarely but are read on every page render; a short TTL
    # avoids one extra RPC per request. Enforcement paths (budget checks) read
    # system_settings:1 transactionally and are unaffected by this cache.
    from django.core.cache import cache

    cached = cache.get(_SYSTEM_SETTINGS_CACHE_KEY)
    if cached is not None:
        # Return a copy: callers may mutate the dict (e.g. settings migration
        # flows), and the cached object must stay pristine for other readers.
        return dict(cached)

    sql = "SELECT * FROM system_settings:1;"
    rows = _first_result(_run(sql))
    if rows:
        result = rows[0]
    else:
        # Create default settings if empty. Provider credentials are deployment-managed.
        default_data = {"monthly_budget_usd": 10.0, "selected_model": "auto", "currency": "auto"}
        rows = _first_result(_run("UPSERT system_settings:1 CONTENT $data;", {"data": default_data}))
        result = rows[0] if rows else default_data
    # Cache a copy so caller-side mutation cannot poison the shared entry.
    cache.set(_SYSTEM_SETTINGS_CACHE_KEY, dict(result), _SYSTEM_SETTINGS_CACHE_TTL)
    return result


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
    sql = "UPSERT system_settings:1 MERGE $data;"
    rows = _first_result(_run(sql, {"data": payload}))
    from django.core.cache import cache

    cache.delete(_SYSTEM_SETTINGS_CACHE_KEY)
    return rows[0] if rows else {}


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
        "BEGIN TRANSACTION;"
        "DELETE FROM documents WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM rag_cache WHERE $doc_uuid INSIDE sources;"
        "DELETE FROM context_cache WHERE doc_uuid = $doc_uuid;"
        "COMMIT TRANSACTION;"
    )
    _run(sql, {"doc_uuid": doc_uuid})


# ── Chunk helpers ─────────────────────────────────────────────────────────────


# Deterministic record ids (doc_uuid:chunk_index) make chunk writes idempotent:
# an ambiguous transport failure retried by _async_run UPSERTs the same records
# instead of duplicating them (same pattern as user_memories, AUD-370).
_CHUNK_UPSERT_MUTATION = (
    "FOR $c IN $payloads { "
    'LET $rec = type::record("chunks", $c.doc_uuid + ":" + <string>$c.chunk_index); '
    "UPSERT $rec MERGE $c; "
    "};"
)


def _write_worker_chunks(doc_uuid: str, payloads: list[dict] | None = None) -> bool:
    """Fence each chunk mutation against cancellation, deletion, and replacement tasks.

    Returns True when the document guard passed and the mutation ran, False when
    the write was fenced out (superseded task, cancelled, or deleted document).
    """
    params = {"doc_uuid": doc_uuid, "expected_task": document_task_name.get(), "payloads": payloads}
    exists = (
        "(SELECT id FROM documents WHERE doc_uuid = $doc_uuid "
        "AND cloud_task_name = $expected_task AND cancel_requested != true)"
    )
    if payloads is None:
        mutation = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
    else:
        mutation = _CHUNK_UPSERT_MUTATION
    sql = f"BEGIN TRANSACTION; LET $guard = array::len({exists}) > 0; IF $guard {{ {mutation} }} RETURN $guard; COMMIT TRANSACTION;"  # nosec B608
    res = _run(sql, params)
    return _transaction_flag(res)


def _transaction_flag(res: Any) -> bool:
    """Extract a boolean LET/RETURN flag from a SurrealDB transaction result."""
    candidates = res if isinstance(res, list) else [res]
    for stmt in candidates:
        if isinstance(stmt, bool):
            return stmt
        if isinstance(stmt, dict):
            val = stmt.get("result", stmt.get("RETURN"))
            if isinstance(val, bool):
                return val
            if isinstance(val, list) and val and isinstance(val[0], bool):
                return val[0]
    return False


def _write_unfenced_chunks(doc_uuid: str, payloads: list[dict] | None = None) -> bool:
    """Non-worker chunk mutation fenced only on document existence.

    Used by regeneration/clone/admin paths that carry no task identity: writes
    are skipped when the document was deleted mid-operation so orphaned chunks
    are never left behind (AUD-354). Returns the transaction guard flag.
    """
    params = {"doc_uuid": doc_uuid, "payloads": payloads}
    exists = "(SELECT id FROM documents WHERE doc_uuid = $doc_uuid)"
    if payloads is None:
        mutation = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
    else:
        mutation = _CHUNK_UPSERT_MUTATION
    sql = f"BEGIN TRANSACTION; LET $guard = array::len({exists}) > 0; IF $guard {{ {mutation} }} RETURN $guard; COMMIT TRANSACTION;"  # nosec B608
    res = _run(sql, params)
    _check_surreal_error(res)
    guard_ok = _transaction_flag(res)
    if not guard_ok:
        logger.warning(
            "[Chunks] Chunk mutation for %s was skipped: document does not exist (deleted mid-operation).",
            doc_uuid,
        )
    return guard_ok


def _write_chunks_guarded(doc_uuid: str, payloads: list[dict] | None = None) -> bool:
    """Dispatch a chunk mutation to the worker-fenced or existence-fenced writer."""
    if document_task_name.get():
        return _write_worker_chunks(doc_uuid, payloads)
    return _write_unfenced_chunks(doc_uuid, payloads)


def _insert_chunk_batches(doc_uuid: str, chunk_payloads: list[dict]) -> bool:
    """Insert chunk payloads in small per-batch transactions.

    Large serialized vector payloads exceed the HTTP request size limit in a
    single call, so each batch is its own transaction. Returns True when any
    write was fenced out.
    """
    batch_size = 15
    fenced_out = False
    for i in range(0, len(chunk_payloads), batch_size):
        batch = chunk_payloads[i : i + batch_size]
        fenced_out = not _write_chunks_guarded(doc_uuid, batch) or fenced_out
    return fenced_out


def _compensating_delete_chunks(doc_uuid: str) -> None:
    """Best-effort chunk cleanup after a mid-insert failure."""
    try:
        _write_chunks_guarded(doc_uuid)
    except Exception as cleanup_err:
        logger.warning(
            "[Chunks] Compensating delete failed for %s after batch insert failure: %s",
            doc_uuid,
            cleanup_err,
        )


def recreate_chunks(doc_uuid: str, chunk_payloads: list[dict]) -> None:
    """
    Replace all chunks for a document.

    The delete commits first, then new chunks are inserted in small batches —
    each batch its own transaction — because large serialized vector payloads
    exceed the HTTP request size limit in a single call. On any batch failure
    the document's chunks are deleted again so a partial set is never left
    behind for RAG to serve as complete. The worker path fences each mutation
    on task identity and cancellation state instead.
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

    # First, atomically delete all existing chunks for this document
    fenced_out = not _write_chunks_guarded(doc_uuid)

    if not chunk_payloads:
        return

    try:
        fenced_out = _insert_chunk_batches(doc_uuid, chunk_payloads) or fenced_out
    except Exception:
        # Batches commit independently (payload-size limit prevents a single
        # transaction), so a mid-loop failure would otherwise leave a partial
        # chunk set that RAG silently serves as complete. Compensate by
        # removing this document's chunks before propagating the failure.
        _compensating_delete_chunks(doc_uuid)
        raise

    if fenced_out:
        # Silent IF-guard skips previously made "success" indistinguishable
        # from "fenced out"; a COMPLETED doc could end up with zero chunks.
        logger.warning(
            "[Chunks] One or more chunk writes for %s were fenced out "
            "(task superseded, document cancelled, or deleted mid-write).",
            doc_uuid,
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


def get_document_all_chunks(doc_uuid: str) -> list[dict[str, Any]]:
    """Retrieve every chunk for a document in one query.

    Unlike get_document_chunks this applies no LIMIT, so callers needing a
    complete, generation-consistent snapshot (e.g. exports) get all rows from
    a single atomic read instead of paginated windows that can straddle a
    concurrent recreate_chunks delete+insert cycle.
    """
    doc_uuid = str(doc_uuid)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT * FROM chunks WHERE doc_uuid = $doc_uuid ORDER BY chunk_index ASC;"
        result = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
        if isinstance(result, list) and result:
            return result
        chunks = _test_chunks.get(doc_uuid, [])
        return sorted(chunks, key=lambda x: int(x.get("chunk_index", 0)))

    sql = "SELECT * FROM chunks WHERE doc_uuid = $doc_uuid ORDER BY chunk_index ASC;"
    result = _first_result(_run(sql, {"doc_uuid": doc_uuid}))
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

    # Single atomic read: paginated windows can straddle a concurrent
    # re-embed's delete+insert cycle and mix two chunk generations into the
    # dedup target (AUD-356 follow-up).
    source_chunks = get_document_all_chunks(source_uuid)
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
    Uses the KNN operator so idx_chunks_hnsw is actually consulted, and excludes
    chunks whose embedding failed to generate (stored as NONE).
    """
    params = {"query_embedding": query_embedding, "limit": limit}
    knn_k = min(max(int(limit), 1) * 3, 100)
    # knn_k is a bounded integer derived from limit; never user-controlled text.
    knn_predicate = f"embedding <|{knn_k},40|> $query_embedding"
    select_fields = (
        "id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id, "
        "vector::distance::knn() AS score "
    )
    where_parts = ["embedding != NONE"]
    if allowed_doc_uuids is not None:
        params["allowed_doc_uuids"] = allowed_doc_uuids
        where_parts.append("doc_uuid INSIDE $allowed_doc_uuids")
    where_parts.append(knn_predicate)
    sql = " ".join(
        [
            f"SELECT {select_fields}",
            "FROM chunks",
            "WHERE " + " AND ".join(where_parts),
            "ORDER BY score ASC",
            "LIMIT $limit;",
        ]
    )
    return _first_result(_run(sql, params))


def _bm25_query_terms(query_text: str, max_terms: int = 8) -> list[str]:
    """Split a query into matchable terms for sparse retrieval (AUD-323).

    Whole-phrase CONTAINS gives near-zero recall for multi-word queries; match
    per-term instead and rank by term coverage.
    """
    import re

    terms: list[str] = []
    seen: set[str] = set()
    for tok in re.split(r"\s+", query_text.strip()):
        term = tok.strip(".,;:!?()[]{}\"'`“”‘’«»-–—")  # noqa: RUF001 -- intentionally strips smart quotes/dashes
        key = term.lower()
        if len(term) >= 2 and key not in seen:
            seen.add(key)
            terms.append(term)
        if len(terms) >= max_terms:
            break
    return terms


def _rank_bm25_rows(rows: list[dict], terms: list[str], limit: int) -> list[dict]:
    """Order candidate rows by number of distinct query terms present."""
    if not terms:
        return rows[:limit]
    lowered = [t.lower() for t in terms]

    def coverage(row: dict) -> int:
        content = str(row.get("content") or "").lower()
        return sum(1 for t in lowered if t in content)

    return sorted(rows, key=coverage, reverse=True)[:limit]


def _bm25_offline_chunks(
    query_text: str, terms: list[str], limit: int, allowed_doc_uuids: list[str] | None
) -> list[dict]:
    chunks = []
    if allowed_doc_uuids is not None:
        for uuid_str in allowed_doc_uuids:
            chunks.extend(_test_chunks.get(uuid_str, []))
    else:
        for c_list in _test_chunks.values():
            chunks.extend(c_list)
    lowered = [t.lower() for t in terms]
    if terms:
        candidates = [c for c in chunks if any(t in str(c.get("content") or "").lower() for t in lowered)]
    else:
        candidates = [c for c in chunks if query_text.lower() in c.get("content", "").lower()]
    return _rank_bm25_rows(candidates, terms, limit)


def _bm25_surreal_params(
    query_text: str, terms: list[str], limit: int, allowed_doc_uuids: list[str] | None
) -> tuple[str, dict]:
    params: dict[str, Any] = {"query_text": query_text, "limit": limit}
    select_cols = (
        "SELECT id, doc_uuid, content, language, chunk_index, page_number, chapter_title, anchor_id FROM chunks "
    )
    if terms:
        # Match any term, then rank by coverage — whole-phrase CONTAINS misses
        # nearly every multi-word query (AUD-323).
        clauses = " OR ".join(f"content CONTAINS $t{i}" for i in range(len(terms)))
        where = f"WHERE ({clauses})"
        params.update({f"t{i}": t for i, t in enumerate(terms)})
        # Over-fetch candidates for ranking; cap to keep the query bounded.
        params["limit"] = max(limit * 5, 50)
    else:
        where = "WHERE content CONTAINS $query_text"
    scope = ""
    if allowed_doc_uuids is not None:
        scope = " AND doc_uuid INSIDE $allowed_doc_uuids"
        params["allowed_doc_uuids"] = allowed_doc_uuids
    return f"{select_cols}{where}{scope} LIMIT $limit;", params  # nosec B608 -- clauses/cols are static; values are bound params


def search_chunks_bm25(query_text: str, limit: int = 10, allowed_doc_uuids: list[str] | None = None) -> list[dict]:
    """Execute keyword search over SurrealDB document chunks for sparse retrieval."""
    terms = _bm25_query_terms(query_text)
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _bm25_offline_chunks(query_text, terms, limit, allowed_doc_uuids)

    sql, params = _bm25_surreal_params(query_text, terms, limit, allowed_doc_uuids)
    rows = _first_result(_run(sql, params))
    return _rank_bm25_rows(rows if isinstance(rows, list) else [], terms, limit)


from extractor.surreal_audit import (  # noqa: F401
    _flush_document_cost,
    _record_document_spend,
    deleted_document_spend_total,
    log_audit,
    monthly_spend_ledger_add,
    monthly_spend_ledger_total,
)
from extractor.surreal_cache import (  # noqa: F401
    RAG_SEARCH_CACHE_PREFIX,
    _extract_stmt_decision,
    _parse_rate_limit_transaction_result,
    _purge_expired_table,
    check_rate_limit_atomic,
    delete_rag_cache_for_document,
    find_chunk_embedding,
    find_chunk_embeddings_batch,
    kv_cache_add,
    kv_cache_delete,
    kv_cache_delete_pattern,
    kv_cache_get,
    kv_cache_set,
    purge_all,
    purge_all_rag_cache,
    purge_expired_kv_cache,
    purge_expired_rag_cache,
    purge_expired_rate_limits,
    search_rag_cache_hnsw,
    upsert_rag_cache,
)
from extractor.surreal_exam import (  # noqa: F401
    FIELD_FSRS_DIFFICULTY,
    _apply_offline_fsrs,
    _normalize_exam_item_payload,
    _offline_get_anchors_by_ids,
    get_anchors_by_ids,
    get_exam_items_by_ids,
    list_exam_items,
    record_attempt,
    save_exam_items,
    update_attempt_rating,
)
from extractor.surreal_memory import (  # noqa: F401
    add_user_memory,
    clear_user_memories,
    count_user_memories,
    purge_expired_user_memories,
    search_user_memories,
)
from extractor.surreal_provenance import (  # noqa: F401
    _build_anchor_payloads,
    _offline_search_anchors,
    _persist_offline_anchors,
    create_anchors_with_provenance,
    create_crystal_with_provenance,
    get_next_source_version,
    list_active_gcs_sources,
    list_crystals_for_user,
    search_anchors_hybrid,
    tombstone_source,
    upsert_source,
)
