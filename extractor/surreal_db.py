"""
SurrealDB REST Client Adapter — AetherOmni v2.0

Implements a thread-safe connection pool using a single global httpx.Client
instance (Gap B-10) to prevent socket exhaustion under concurrent request load.

All public methods execute SurrealQL via the /sql endpoint and return
plain Python dicts/lists — no ORM coupling.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC
from typing import Any

import httpx
from django.conf import settings
from websockets.sync.client import connect

logger = logging.getLogger(__name__)

# ── Singleton connection pool ──────────────────────────────────────────────────
# A single httpx.Client is shared across all threads (safe per httpx docs).
# It maintains an internal connection pool, reusing TCP sockets on Keep-Alive.
_client_lock = threading.Lock()
_client: Any | None = None


_test_chunks: dict[str, list[dict]] = {}


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
        "created_at": doc.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if doc.created_at else None,
        "updated_at": doc.updated_at.strftime("%Y-%m-%dT%H:%M:%SZ") if doc.updated_at else None,
        "expires_at": doc.expires_at.strftime("%Y-%m-%dT%H:%M:%SZ") if doc.expires_at else None,
    }


def _settings_to_dict(settings_obj) -> dict:
    if not settings_obj:
        return {}
    return {
        "monthly_budget_usd": float(settings_obj.monthly_budget_usd),
        "selected_model": settings_obj.selected_model,
        "currency": settings_obj.currency,
        "openrouter_api_key": settings_obj.openrouter_api_key,
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


class SurrealWebSocketClient:
    """Wrapper that emulates httpx.Client interface but communicates over WebSocket JSON-RPC."""

    def __init__(self, base_url: str, user: str, token_pass: str):
        self.base_url = base_url
        self.user = user
        self.token_pass = token_pass

        # Translate HTTP URL to WebSocket RPC URL
        ws_url = base_url.replace("http://", "ws://").replace("https://", "wss://")
        if not ws_url.endswith("/rpc"):
            ws_url = ws_url.rstrip("/") + "/rpc"
        self.ws_url = ws_url

    def get(self, path: str, **kwargs) -> Any:
        if path == "/health":
            try:
                with connect(self.ws_url, timeout=3.0):

                    class HealthResponse:
                        status_code = 200

                    return HealthResponse()
            except Exception as e:
                logger.warning("[SurrealDB] WebSocket health check failed: %s", e)

                class FailedResponse:
                    status_code = 500

                return FailedResponse()
        raise NotImplementedError("Only /health is supported for GET.")

    def post(self, path: str, content: bytes, headers: dict | None = None, **kwargs) -> Any:
        if path != "/sql":
            raise NotImplementedError("Only /sql is supported for POST.")

        sql_body = content.decode("utf-8")
        ns = headers.get("NS", "omnirag") if headers else "omnirag"
        db = headers.get("DB", "extractor") if headers else "extractor"

        try:
            with connect(self.ws_url, timeout=30.0) as websocket:
                # 1. Signin
                signin_payload = {
                    "id": "signin",
                    "method": "signin",
                    "params": [{"user": self.user, "pass": self.token_pass}],
                }
                websocket.send(json.dumps(signin_payload))
                resp1 = json.loads(websocket.recv())
                if "error" in resp1:
                    raise RuntimeError(f"SurrealDB Signin Error: {resp1['error']}")

                # 2. Use NS and DB
                use_payload = {"id": "use", "method": "use", "params": [ns, db]}
                websocket.send(json.dumps(use_payload))
                resp2 = json.loads(websocket.recv())
                if "error" in resp2:
                    raise RuntimeError(f"SurrealDB Use Error: {resp2['error']}")

                # 3. Execute SQL Query
                query_payload = {"id": "query", "method": "query", "params": [sql_body, {}]}
                websocket.send(json.dumps(query_payload))
                resp3 = json.loads(websocket.recv())
                if "error" in resp3:
                    raise RuntimeError(f"SurrealDB Query Error: {resp3['error']}")

                ws_result = resp3.get("result", [])

                class QueryResponse:
                    status_code = 200
                    text = ""

                    def raise_for_status(self):
                        pass

                    def json(self):
                        return ws_result

                return QueryResponse()

        except Exception as exc:
            logger.exception("[SurrealDB] WebSocket query failure: %s", exc)
            raise


def get_surreal_client() -> Any:
    """Return the process-level shared SurrealWebSocketClient, initialising lazily."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked locking
                surreal_url = getattr(settings, "SURREAL_URL", os.getenv("SURREAL_URL", "http://localhost:8001"))
                surreal_user = getattr(settings, "SURREAL_USER", os.getenv("SURREAL_USER", "root"))
                surreal_pass = getattr(settings, "SURREAL_PASS", os.getenv("SURREAL_PASS", "root"))
                _client = SurrealWebSocketClient(
                    base_url=surreal_url,
                    user=surreal_user,
                    token_pass=surreal_pass,
                )
                logger.info("[SurrealDB] WebSocket Connection pool initialised: %s", surreal_url)
    return _client


def _get_client() -> Any:
    return get_surreal_client()


def _headers() -> dict[str, str]:
    """Return required SurrealDB namespace/database scope headers."""
    ns = getattr(settings, "SURREAL_NS", os.getenv("SURREAL_NS", "omnirag"))
    db = getattr(settings, "SURREAL_DB", os.getenv("SURREAL_DB", "extractor"))
    return {
        "NS": ns,
        "DB": db,
        "surreal-ns": ns,
        "surreal-db": db,
        "Accept": "application/json",
        "Content-Type": "text/plain",
    }


def _run(sql: str, params: dict | None = None) -> list[dict]:
    """
    Execute one or more SurrealQL statements and return the parsed result array.
    Raises RuntimeError on HTTP or SurrealDB-level errors.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [{"status": "OK", "result": []}]
    client = _get_client()

    let_sqls = []
    num_params = 0
    if params:
        for k, v in params.items():
            let_sqls.append(f"LET ${k} = {json.dumps(v)};")
        num_params = len(params)
        body = "\n".join(let_sqls) + "\n" + sql
    else:
        body = sql

    try:
        resp = client.post("/sql", content=body.encode(), headers=_headers())
        resp.raise_for_status()
        results = resp.json()
        # SurrealDB wraps each statement in {status, result} — unwrap errors
        for stmt in results:
            if stmt.get("status") == "ERR":
                raise RuntimeError(f"SurrealDB error: {stmt.get('detail', stmt)}")
        # Slice out the LET statements' results so that the caller gets exactly the query statements' results
        return results[num_params:]
    except httpx.HTTPStatusError as exc:
        logger.exception("[SurrealDB] HTTP %s for SQL: %s", exc.response.status_code, sql[:120])
        raise
    except httpx.RequestError:
        logger.exception("[SurrealDB] Connection error.")
        raise


def _first_result(results: list[dict]) -> list[Any]:
    """Extract the first statement's result list."""
    if results:
        return results[0].get("result", [])
    return []


# ── Health check ───────────────────────────────────────────────────────────────


def check_health() -> bool:
    """Return True if SurrealDB /health endpoint returns HTTP 200, else False."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return False
    try:
        client = _get_client()
        resp = client.get("/health")
        return resp.status_code == 200
    except Exception:  # — health probe must absorb any SDK or network failure
        return False


# ── Document helpers ──────────────────────────────────────────


def create_document(data: dict) -> dict:
    """Create a new document metadata record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.contrib.auth import get_user_model

        from extractor.models import SourceDocument

        User = get_user_model()

        uploaded_by = None
        uid = data.get("uploaded_by_id")
        if uid:
            try:
                uploaded_by = User.objects.get(id=uid)
            except User.DoesNotExist:
                pass

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

    payload = {k: v for k, v in data.items() if v is not None}
    fields = []
    params = {}
    for k, v in payload.items():
        if k in ("created_at", "updated_at", "expires_at"):
            fields.append(f"{k}: <datetime> ${k}")
            params[k] = v
        else:
            fields.append(f"{k}: ${k}")
            params[k] = v
    sql = f"INSERT INTO documents {{ {', '.join(fields)} }};"
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else {}


def update_document(doc_uuid: str, data: dict) -> dict:
    """Update fields on a document record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.contrib.auth import get_user_model

        from extractor.models import SourceDocument

        User = get_user_model()
        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(doc_uuid))
        except (SourceDocument.DoesNotExist, ValueError):
            return {}

        for k, v in data.items():
            if k == "uploaded_by_id":
                if v:
                    try:
                        doc.uploaded_by = User.objects.get(id=v)
                    except User.DoesNotExist:
                        pass
                else:
                    doc.uploaded_by = None
            elif k == "expires_at":
                if v:
                    from django.utils.dateparse import parse_datetime as django_parse

                    doc.expires_at = django_parse(v)
                else:
                    doc.expires_at = None
            elif hasattr(doc, k):
                setattr(doc, k, v)
        doc.save()
        return _model_to_dict(doc)

    payload = {k: v for k, v in data.items() if v is not None}
    if not payload:
        return get_document(doc_uuid) or {}

    set_parts = []
    params = {"doc_uuid": doc_uuid}
    for k, v in payload.items():
        if k in ("created_at", "updated_at", "expires_at"):
            set_parts.append(f"{k} = <datetime> ${k}")
            params[k] = v
        else:
            set_parts.append(f"{k} = ${k}")
            params[k] = v

    if "updated_at" not in payload:
        set_parts.append("updated_at = time::now()")

    sql = f"UPDATE documents SET {', '.join(set_parts)} WHERE doc_uuid = $doc_uuid;"  # nosec B608 # noqa: S608
    rows = _first_result(_run(sql, params))
    return rows[0] if rows else {}


def get_document(doc_uuid: str) -> dict | None:
    """Retrieve a single document record by UUID."""
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
        except (SourceDocument.DoesNotExist, ValueError):
            return None

    sql = "SELECT * FROM documents WHERE doc_uuid = $doc_uuid;"  # nosec B608
    results = _run(sql, {"doc_uuid": doc_uuid})
    rows = _first_result(results)
    return rows[0] if rows else None


def list_documents(user_id: str | None = None) -> list[dict]:
    """Retrieve documents from SurrealDB (user-specific + public)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        if user_id:
            from django.db.models import Q

            qs = SourceDocument.objects.filter(Q(uploaded_by_id=user_id) | Q(uploaded_by__isnull=True))
        else:
            qs = SourceDocument.objects.all()
        return [_model_to_dict(doc) for doc in qs]

    if user_id:
        sql = (
            "SELECT * FROM documents WHERE uploaded_by_id = $user_id OR uploaded_by_id = NONE ORDER BY created_at DESC;"
        )
        return _first_result(_run(sql, {"user_id": str(user_id)}))
    else:
        sql = "SELECT * FROM documents ORDER BY created_at DESC;"
        return _first_result(_run(sql))


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

    # Create default settings if empty
    default_data = {"monthly_budget_usd": 10.0, "selected_model": "auto", "currency": "auto", "openrouter_api_key": ""}
    rows = _first_result(_run("UPSERT system_settings:1 CONTENT $data;", {"data": default_data}))
    return rows[0] if rows else default_data


def save_system_settings(data: dict) -> dict:
    """Upsert system settings record in SurrealDB."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SystemSettings

        s = SystemSettings.get_settings()
        for k, v in data.items():
            if hasattr(s, k):
                setattr(s, k, v)
        s.save()
        return _settings_to_dict(s)

    payload = {k: v for k, v in data.items() if v is not None}
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


def delete_document(doc_uuid: str) -> None:
    """Delete document record and all associated chunks/cache entries."""
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
            doc.delete()
        except (SourceDocument.DoesNotExist, ValueError):
            pass
        return

    sql = (  # nosec B608
        "DELETE FROM documents WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"
        "DELETE FROM rag_cache WHERE $doc_uuid INSIDE sources;"
    )
    _run(sql, {"doc_uuid": doc_uuid})


# ── Chunk helpers ─────────────────────────────────────────────────────────────


def recreate_chunks(doc_uuid: str, chunk_payloads: list[dict]) -> None:
    """
    Atomically replace all chunks for a document.
    Deletes existing chunks first, then bulk-inserts new ones in a single statement.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks[doc_uuid] = chunk_payloads
        # Execute mock run if patched in tests
        _run("INSERT INTO chunks $payloads;", {"payloads": chunk_payloads})
        return

    delete_chunks(doc_uuid)
    if not chunk_payloads:
        return
    for chunk in chunk_payloads:
        chunk["doc_uuid"] = doc_uuid

    # Bulk insert in a single network request to prevent HTTP connection timeouts
    _run("INSERT INTO chunks $payloads;", {"payloads": chunk_payloads})


def delete_chunks(doc_uuid: str) -> None:
    """Remove all vector chunks for a document."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks.pop(doc_uuid, None)
        _run("DELETE FROM chunks WHERE doc_uuid = $doc_uuid;", {"doc_uuid": doc_uuid})
        return

    sql = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"  # nosec B608
    _run(sql, {"doc_uuid": doc_uuid})


def count_document_chunks(doc_uuid: str) -> int:
    """Returns the number of chunks stored in SurrealDB for the given doc_uuid."""
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


def count_documents_chunks(doc_uuids: list[str]) -> dict[str, int]:
    """
    Returns a dictionary mapping doc_uuid to the number of chunks stored in SurrealDB
    for each doc_uuid in the provided list.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
        results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
        if results:
            counts = dict.fromkeys(doc_uuids, 0)
            for row in results:
                uuid = row.get("doc_uuid")
                if uuid:
                    counts[uuid] = row.get("n", 0)
            return counts
        return {u: len(_test_chunks.get(u, [])) for u in doc_uuids}

    if not doc_uuids:
        return {}
    sql = "SELECT count() AS n, doc_uuid FROM chunks WHERE doc_uuid INSIDE $doc_uuids GROUP BY doc_uuid;"
    results = _first_result(_run(sql, {"doc_uuids": doc_uuids}))
    counts = dict.fromkeys(doc_uuids, 0)
    if results:
        for row in results:
            uuid = row.get("doc_uuid")
            if uuid:
                counts[uuid] = row.get("n", 0)
    return counts


def clone_chunks(source_uuid: str, target_uuid: str) -> None:
    """Copy all chunks from source_uuid to target_uuid (deduplication flow)."""
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        _test_chunks[target_uuid] = list(_test_chunks.get(source_uuid, []))
        _run(
            "INSERT INTO chunks SELECT * FROM chunks WHERE doc_uuid = $source_uuid;",
            {"source_uuid": source_uuid, "target_uuid": target_uuid},
        )
        return

    sql = (  # nosec B608
        "LET $rows = (SELECT * FROM chunks WHERE doc_uuid = $source_uuid);"
        "FOR $row IN $rows {"
        "  INSERT INTO chunks {"
        "    doc_uuid: $target_uuid,"
        "    chunk_index: $row.chunk_index,"
        "    content: $row.content,"
        "    token_count: $row.token_count,"
        "    language: $row.language,"
        "    embedding: $row.embedding"
        "  };"
        "};"
    )
    _run(sql, {"source_uuid": source_uuid, "target_uuid": target_uuid})


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
        sql = (  # nosec B608
            "SELECT id, doc_uuid, content, language, chunk_index, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "WHERE doc_uuid INSIDE $allowed_doc_uuids "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    else:
        sql = (  # nosec B608
            "SELECT id, doc_uuid, content, language, chunk_index, "
            "1.0 - vector::similarity::cosine(embedding, $query_embedding) AS score "
            "FROM chunks "
            "ORDER BY score ASC "
            "LIMIT $limit;"
        )
    return _first_result(_run(sql, params))


# ── RAG cache helpers ─────────────────────────────────────────────────────────


def upsert_rag_cache(
    user_id: str, query_text: str, query_embedding: list[float], answer_text: str, sources: list[str]
) -> None:
    """Insert a new semantic cache entry."""
    sql = (
        "INSERT INTO rag_cache {"
        "  user_id: $user_id,"
        "  query_text: $query_text,"
        "  query_embedding: $query_embedding,"
        "  answer_text: $answer_text,"
        "  sources: $sources"
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
        },
    )


def search_rag_cache_hnsw(
    user_id: str, query_embedding: list[float], threshold: float = 0.15, limit: int = 1
) -> list[dict]:
    """Search for a semantically similar cached answer for this user."""
    sql = (  # nosec B608
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

    _run("DELETE FROM documents; DELETE FROM chunks; DELETE FROM rag_cache; DELETE FROM user_memories;")


# ── KV cache helpers ──────────────────────────────────────────────────────────


def kv_cache_get(key: str) -> Any | None:
    """Retrieve a cached value by key. Returns None on miss or expiry."""
    sql = (  # nosec B608
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
            except (json.JSONDecodeError, ValueError):
                return val_data
    return None


def kv_cache_set(key: str, value: Any, ttl_seconds: int | None = None) -> None:
    """Store a value in the KV cache with optional TTL."""
    from datetime import datetime, timedelta

    if ttl_seconds is not None:
        expires_at_val = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
    else:
        expires_at_val = None

    json_value = json.dumps(value)
    sql = (  # nosec B608
        "DELETE FROM kv_cache WHERE cache_key = $cache_key;"
        "INSERT INTO kv_cache {"
        "  cache_key: $cache_key,"
        "  cache_value: $cache_value,"
        "  expires_at: $expires_at"
        "};"
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
    sql = "DELETE FROM kv_cache WHERE string::starts_with(cache_key, $prefix);"  # nosec B608
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
    sql = (  # nosec B608
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
        "metadata": json.dumps(metadata or {}),
        "ip_address": ip_address,
    }
    if doc_uuid is not None:
        params["doc_uuid"] = str(doc_uuid)
        sql = (
            "INSERT INTO audit_logs {"
            "  action: $action,"
            "  user_id: $user_id,"
            "  doc_uuid: $doc_uuid,"
            "  metadata: $metadata,"
            "  ip_address: $ip_address"
            "};"
        )
    else:
        sql = (
            "INSERT INTO audit_logs {"
            "  action: $action,"
            "  user_id: $user_id,"
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
