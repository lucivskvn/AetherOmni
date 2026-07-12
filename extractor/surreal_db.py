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

logger = logging.getLogger(__name__)

# ── Singleton connection pool ──────────────────────────────────────────────────
# A single httpx.Client is shared across all threads (safe per httpx docs).
# It maintains an internal connection pool, reusing TCP sockets on Keep-Alive.
_client_lock = threading.Lock()
_client: httpx.Client | None = None


def get_surreal_client() -> httpx.Client:
    """Return the process-level shared httpx.Client, initialising lazily."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:  # double-checked locking
                surreal_url = getattr(settings, "SURREAL_URL", os.getenv("SURREAL_URL", "http://localhost:8001"))
                surreal_user = getattr(settings, "SURREAL_USER", os.getenv("SURREAL_USER", "root"))
                surreal_pass = getattr(settings, "SURREAL_PASS", os.getenv("SURREAL_PASS", "root"))
                _client = httpx.Client(
                    base_url=surreal_url,
                    auth=(surreal_user, surreal_pass),
                    timeout=30.0,
                    limits=httpx.Limits(max_connections=50, max_keepalive_connections=25),
                )
                logger.info("[SurrealDB] Connection pool initialised: %s", surreal_url)
    return _client


def _get_client() -> httpx.Client:
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
    except Exception:
        return False


# ── Document helpers ──────────────────────────────────────────────────────────


def upsert_document(doc_uuid: str, data: dict) -> None:
    """Create or update a document metadata record in SurrealDB."""
    set_parts = []
    for k in data.keys():
        set_parts.append(f"{k} = ${k}")
    sql = f"UPDATE documents SET {', '.join(set_parts)} WHERE doc_uuid = $doc_uuid;"  # nosec B608 # noqa: S608
    params = {"doc_uuid": doc_uuid, **data}
    rows = _first_result(_run(sql, params))
    if not rows:
        # INSERT if UPDATE affected 0 rows (first write)
        insert_sql = "INSERT INTO documents " + _insert_clause(params) + ";"  # nosec B608
        _run(insert_sql)


def get_document(doc_uuid: str) -> dict | None:
    """Retrieve a single document record by UUID."""
    sql = "SELECT * FROM documents WHERE doc_uuid = $doc_uuid;"  # nosec B608
    results = _run(sql, {"doc_uuid": doc_uuid})
    rows = _first_result(results)
    return rows[0] if rows else None


def delete_document(doc_uuid: str) -> None:
    """Delete document record and all associated chunks/cache entries."""
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
    delete_chunks(doc_uuid)
    if not chunk_payloads:
        return
    for chunk in chunk_payloads:
        chunk["doc_uuid"] = doc_uuid

    # Bulk insert in a single network request to prevent HTTP connection timeouts
    _run("INSERT INTO chunks $payloads;", {"payloads": chunk_payloads})


def delete_chunks(doc_uuid: str) -> None:
    """Remove all vector chunks for a document."""
    sql = "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;"  # nosec B608
    _run(sql, {"doc_uuid": doc_uuid})


def count_document_chunks(doc_uuid: str) -> int:
    """Returns the number of chunks stored in SurrealDB for the given doc_uuid."""
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
            except Exception:
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


# ── Internal SQL construction helpers ─────────────────────────────────────────


def _set_clause(data: dict) -> str:
    """Build a SurrealQL SET clause from a dict. Example: 'a = 1, b = \"x\"'"""
    parts = []
    for k, v in data.items():
        parts.append(f"{k} = {json.dumps(v)}")
    return ", ".join(parts)


def _insert_clause(data: dict) -> str:
    """Build a SurrealQL INSERT object literal from a dict."""
    parts = []
    for k, v in data.items():
        parts.append(f"{k}: {json.dumps(v)}")
    return "{ " + ", ".join(parts) + " }"
