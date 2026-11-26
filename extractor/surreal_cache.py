"""
KORDA SurrealDB Cache & Rate-Limit Adapter

Semantic RAG cache, KV cache, context cache, chunk-embedding reuse
lookups, and atomic sliding-window rate limiting split out of
``surreal_db.py``. Infrastructure resolves through
``extractor.surreal_db`` at call time to preserve mock.patch semantics.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from django.conf import settings

logger = logging.getLogger(__name__)

# ── RAG cache helpers ─────────────────────────────────────────────────────────


def upsert_rag_cache(
    user_id: str,
    query_text: str,
    query_embedding: list[float],
    answer_text: str,
    sources: list[str],
    ttl_seconds: int = 604800,
) -> None:
    from extractor import surreal_db

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
        "  expires_at: $expires_at"
        "};"
    )
    surreal_db._run(
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
    from extractor import surreal_db

    """Search for a semantically similar cached answer for this user."""
    knn_k = min(max(int(limit), 1) * 4, 50)
    # knn_k is a bounded integer derived from limit; never user-controlled text.
    knn_predicate = f"query_embedding <|{knn_k},40|> $query_embedding"
    sql = " ".join(
        [
            "SELECT id, answer_text, sources, query_text,",
            "vector::distance::knn() AS score",
            "FROM rag_cache",
            "WHERE user_id = $user_id AND expires_at > time::now()",
            "AND " + knn_predicate,
            "ORDER BY score ASC",
            "LIMIT $limit;",
        ]
    )
    results = surreal_db._first_result(
        surreal_db._run(
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
    from extractor import surreal_db

    """Delete all expired RAG cache entries. Returns count of deleted rows."""
    sql = "SELECT count() AS n FROM rag_cache WHERE expires_at < time::now() GROUP ALL;"
    count_rows = surreal_db._first_result(surreal_db._run(sql))
    count = count_rows[0].get("n", 0) if count_rows else 0
    surreal_db._run("DELETE FROM rag_cache WHERE expires_at < time::now();")
    return count


def purge_all_rag_cache() -> None:
    from extractor import surreal_db

    """Delete all entries from rag_cache table."""
    surreal_db._run("DELETE FROM rag_cache;")


def _purge_expired_table(table: str) -> int:
    from extractor import surreal_db

    """Delete expired rows from a table; rows with expires_at NONE are retained."""
    count_sql = f"SELECT count() AS n FROM {table} WHERE expires_at < time::now() GROUP ALL;"  # nosec B608 # noqa: S608 -- table is a static literal
    count_rows = surreal_db._first_result(surreal_db._run(count_sql))
    count = count_rows[0].get("n", 0) if count_rows else 0
    surreal_db._run(f"DELETE FROM {table} WHERE expires_at < time::now();")  # nosec B608 # noqa: S608 -- table is a static literal
    return count


def purge_expired_kv_cache() -> int:
    """Delete expired kv_cache rows (e.g. 24h RAG search cache); permanent entries are kept."""
    return _purge_expired_table("kv_cache")


def purge_expired_rate_limits() -> int:
    """Delete expired rate_limits windows for keys that are never reused."""
    return _purge_expired_table("rate_limits")


def delete_rag_cache_for_document(doc_uuid: str) -> None:
    from extractor import surreal_db

    """Delete semantic-cache rows whose sources include the given document.

    Used when document content changes (edit/reprocess) so stale grounded
    answers are not served for up to the 7-day TTL.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return
    surreal_db._run("DELETE FROM rag_cache WHERE $doc_uuid INSIDE sources;", {"doc_uuid": str(doc_uuid)})


def purge_all() -> None:
    from extractor import surreal_db

    """Delete all user-derived knowledge records, retaining financial and audit data.

    Covers the full derived corpus — documents, chunks, rag_cache, sources,
    anchors, crystals, exam items/attempts, entity graph, context cache, and
    user memories — while preserving monthly_spend_ledger,
    document_spend_reconciliations, audit_logs, system_settings, and kv_cache
    (which carries spend/settings state required for budget accounting).
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        SourceDocument.objects.all().delete()
        surreal_db._test_chunks.clear()
        surreal_db._test_korda_sources.clear()
        surreal_db._test_korda_anchors.clear()
        surreal_db._test_korda_crystals.clear()
        surreal_db._test_korda_exam_items.clear()
        surreal_db._test_korda_attempts.clear()
        return

    documents = surreal_db.list_documents()
    if not all(surreal_db._flush_document_cost(document) for document in documents):
        raise RuntimeError("Document costs could not be recorded; purge aborted.")
    surreal_db._run(
        "DELETE FROM documents; DELETE FROM chunks; DELETE FROM rag_cache; DELETE FROM user_memories; "
        "DELETE FROM source; DELETE FROM anchor; DELETE FROM anchored_in; "
        "DELETE FROM crystal; DELETE FROM grounded_in; "
        "DELETE FROM exam_item; DELETE FROM attempt; "
        "DELETE FROM entities; DELETE FROM chunk_references; DELETE FROM entity_relations; "
        "DELETE FROM context_cache;"
    )


# ── KV cache helpers ──────────────────────────────────────────────────────────


def kv_cache_get(key: str) -> Any | None:
    from extractor import surreal_db

    """Retrieve a cached value by key. Returns None on miss or expiry."""
    sql = (
        "SELECT cache_value FROM kv_cache "
        "WHERE cache_key = $cache_key AND (expires_at IS NONE OR expires_at > time::now());"
    )
    rows = surreal_db._first_result(surreal_db._run(sql, {"cache_key": key}))
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
    from extractor import surreal_db

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
    surreal_db._run(
        sql,
        {
            "cache_key": key,
            "cache_value": json_value,
            "expires_at": expires_at_val,
        },
    )


RAG_SEARCH_CACHE_PREFIX = "rag_search_cache:"


def kv_cache_delete_pattern(prefix: str) -> None:
    from extractor import surreal_db

    """Delete all KV entries whose key starts with the given prefix."""
    sql = "DELETE FROM kv_cache WHERE string::starts_with(cache_key, $prefix);"
    surreal_db._run(sql, {"prefix": prefix})


def find_chunk_embedding(text: str) -> list[float] | None:
    from extractor import surreal_db

    """Finds an existing embedding for the exact text block from any previously ingested document."""
    sql = "SELECT embedding FROM chunks WHERE content = $text LIMIT 1;"
    results = surreal_db._first_result(surreal_db._run(sql, {"text": text}))
    if results and isinstance(results[0], dict) and "embedding" in results[0]:
        return results[0]["embedding"]
    return None


def find_chunk_embeddings_batch(texts: list[str]) -> dict[str, list[float]]:
    from extractor import surreal_db

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
    results = surreal_db._first_result(surreal_db._run(sql, {"texts": unique_texts}))
    mapping: dict[str, list[float]] = {}
    if results and isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and "content" in row and "embedding" in row:
                mapping[row["content"]] = row["embedding"]
    return mapping


# ── Context Caching & High-Throughput Rate Limits ───────────────────────────


def context_cache_get(context_hash: str) -> dict[str, Any] | None:
    from extractor import surreal_db

    """Retrieve cached context prefix or prompt by hash and increment hit counter."""
    sql = "SELECT * FROM context_cache WHERE context_hash = $context_hash AND expires_at > time::now() LIMIT 1;"
    rows = surreal_db._first_result(surreal_db._run(sql, {"context_hash": context_hash}))
    if rows and isinstance(rows, list) and len(rows) > 0:
        surreal_db._run(
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
    from extractor import surreal_db

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
    sql = (
        "BEGIN TRANSACTION;"
        "DELETE FROM context_cache WHERE context_hash = $context_hash;"
        "INSERT INTO context_cache $payload;"
        "COMMIT TRANSACTION;"
    )
    surreal_db._run(sql, {"context_hash": context_hash, "payload": payload})


def check_rate_limit_atomic(key: str, max_requests: int, window_seconds: int = 3600) -> bool:
    from extractor import surreal_db

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
            "DELETE FROM rate_limits WHERE key = $key AND expires_at <= time::now();"
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
        res = surreal_db._run(sql, {"key": key, "max_requests": max_requests, "expires_at": expires_at})
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
    from extractor import surreal_db

    """Extract boolean decision from SurrealDB rate limit transaction statement results."""
    if isinstance(res, bool):
        return res
    candidates = res if isinstance(res, list) else [res]
    for stmt in candidates:
        if isinstance(stmt, bool):
            return stmt
        if isinstance(stmt, dict):
            surreal_db._check_surreal_error(stmt)
            decision = _extract_stmt_decision(stmt.get("result"), max_requests)
            if decision is not None:
                return decision
    return None
