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
    """Delete all expired RAG cache entries. Returns count of deleted rows."""
    return _purge_expired_table("rag_cache")


def purge_all_rag_cache() -> None:
    from extractor import surreal_db

    """Delete all entries from rag_cache table."""
    surreal_db._run("DELETE FROM rag_cache;")


def _purge_expired_table(table: str) -> int:
    from extractor import surreal_db

    """Delete expired rows from a table; rows with expires_at NONE are retained.

    Count and delete run in one transaction so the reported count matches the
    deleted set's snapshot (AUD-375).
    """
    sql = (
        "BEGIN TRANSACTION;"  # noqa: S608 -- table is a static literal
        f"LET $c = (SELECT count() AS n FROM {table} WHERE expires_at < time::now() GROUP ALL);"  # nosec B608
        f"DELETE FROM {table} WHERE expires_at < time::now();"  # nosec B608
        "RETURN $c;"
        "COMMIT TRANSACTION;"
    )
    res = surreal_db._run(sql)
    surreal_db._check_surreal_error(res)
    found = _find_purge_count(res)
    return found if found is not None else 0


def _find_purge_count(node: Any) -> int | None:
    """Recursively locate the {n: count} row inside a transaction result."""
    if isinstance(node, dict):
        if "n" in node:
            return int(node.get("n") or 0)
        for val in node.values():
            found = _find_purge_count(val)
            if found is not None:
                return found
    elif isinstance(node, list):
        for val in node:
            found = _find_purge_count(val)
            if found is not None:
                return found
    return None


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
    anchors, crystals, exam items/attempts (including their tests and
    attempted_on graph edges), entity graph, context cache, and user
    memories — while preserving monthly_spend_ledger,
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
        "DELETE FROM exam_item; DELETE FROM attempt; DELETE FROM tests; DELETE FROM attempted_on; "
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
    try:
        # json.dumps inside the try: a non-serializable value must degrade to
        # a skipped cache write, not propagate to callers (AUD-373).
        json_value = json.dumps(value)
        surreal_db._run(
            sql,
            {
                "cache_key": key,
                "cache_value": json_value,
                "expires_at": expires_at_val,
            },
        )
    except Exception as exc:
        # Cache writes are best-effort: a same-key race losing the UNIQUE
        # constraint must not propagate to callers (AUD-328).
        logger.debug("[KVCache] Set failed for key %r: %s", key, exc)


RAG_SEARCH_CACHE_PREFIX = "rag_search_cache:"


def kv_cache_add(key: str, value: Any, ttl_seconds: int | None = None) -> bool:
    """INSERT-only KV write: returns False when the key already exists.

    Unlike kv_cache_set this never overwrites, so it can serve as a
    cross-worker, cross-instance mutual-exclusion primitive (the unique
    cache_key index makes the check-and-set atomic).
    """
    from datetime import timedelta

    from extractor import surreal_db

    expires_at_val = datetime.now(UTC) + timedelta(seconds=ttl_seconds) if ttl_seconds is not None else None
    sql = "INSERT INTO kv_cache {  cache_key: $cache_key,  cache_value: $cache_value,  expires_at: $expires_at};"
    try:
        surreal_db._run(
            sql,
            {
                "cache_key": key,
                "cache_value": json.dumps(value),
                "expires_at": expires_at_val,
            },
        )
        return True
    except Exception:
        return False


def kv_cache_delete(key: str) -> None:
    """Delete a single KV entry by exact key."""
    from extractor import surreal_db

    surreal_db._run("DELETE FROM kv_cache WHERE cache_key = $cache_key;", {"cache_key": key})


def kv_cache_delete_pattern(prefix: str) -> None:
    from extractor import surreal_db

    """Delete all KV entries whose key starts with the given prefix."""
    sql = "DELETE FROM kv_cache WHERE string::starts_with(cache_key, $prefix);"
    surreal_db._run(sql, {"prefix": prefix})


def find_chunk_embedding(text: str) -> list[float] | None:
    from extractor import surreal_db

    """Finds an existing embedding for the exact text block from any previously ingested document."""
    # embedding != NONE: a chunk row without an embedding must not shadow a
    # reusable one for identical content (AUD-372).
    sql = "SELECT embedding FROM chunks WHERE content = $text AND embedding != NONE LIMIT 1;"
    results = surreal_db._first_result(surreal_db._run(sql, {"text": text}))
    if results and isinstance(results[0], dict):
        emb = results[0].get("embedding")
        if emb is not None:
            return emb
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
    sql = "SELECT content, embedding FROM chunks WHERE content IN $texts AND embedding != NONE;"
    results = surreal_db._first_result(surreal_db._run(sql, {"texts": unique_texts}))
    mapping: dict[str, list[float]] = {}
    if results and isinstance(results, list):
        for row in results:
            if isinstance(row, dict) and "content" in row and row.get("embedding") is not None:
                mapping[row["content"]] = row["embedding"]
    return mapping


# ── High-Throughput Rate Limits ──────────────────────────────────────────────


def check_rate_limit_atomic(key: str, max_requests: int, window_seconds: int = 3600) -> bool:
    from extractor import surreal_db

    """
    Atomic fixed-window rate limit checker in SurrealDB (window anchored at
    first request, not sliding). Returns True if allowed, False if exceeded.
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
        params = {"key": key, "max_requests": max_requests, "expires_at": expires_at}
        # The key column is UNIQUE-indexed: under concurrent first-requests the
        # loser's INSERT aborts the transaction. Retry once so the second
        # attempt observes the committed row and returns a correct decision
        # instead of a spurious deny (AUD-374).
        decision = None
        last_exc: Exception | None = None
        for _attempt in range(2):
            try:
                res = surreal_db._run(sql, params)
                decision = _parse_rate_limit_transaction_result(res, max_requests)
                break
            except Exception as exc:
                last_exc = exc
                logger.debug("[RateLimit] Transaction attempt %d failed: %s", _attempt + 1, exc)
        if last_exc is not None and decision is None:
            raise last_exc
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
