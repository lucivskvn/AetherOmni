"""
KORDA SurrealDB User-Memory Adapter

Semantic user memory helpers split out of ``surreal_db.py``.
Infrastructure resolves through ``extractor.surreal_db`` at call time to
preserve mock.patch semantics.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

logger = logging.getLogger(__name__)

# Memories are personal preference data; retain them on a bounded TTL rather
# than forever. Legacy rows written before expires_at existed carry NONE and
# are treated as expired by both search and purge.
_DEFAULT_MEMORY_TTL_DAYS = 180


def _memory_ttl_days() -> int:
    try:
        return max(1, int(os.getenv("USER_MEMORY_TTL_DAYS", _DEFAULT_MEMORY_TTL_DAYS)))
    except ValueError:
        return _DEFAULT_MEMORY_TTL_DAYS


def _memory_expires_at() -> datetime:
    return datetime.now(UTC) + timedelta(days=_memory_ttl_days())


# ── User memory helpers ───────────────────────────────────────────────────────


def add_user_memory(user_id: str, memory_text: str, embedding: list[float]) -> None:
    from extractor import surreal_db

    """Persist a new user memory vector with a bounded retention window."""
    sql = (
        "INSERT INTO user_memories {"
        "  user_id: $user_id,"
        "  memory_text: $memory_text,"
        "  embedding: $embedding,"
        "  expires_at: $expires_at"
        "};"
    )
    surreal_db._run(
        sql,
        {
            "user_id": user_id,
            "memory_text": memory_text,
            "embedding": embedding,
            "expires_at": _memory_expires_at(),
        },
    )


def search_user_memories(user_id: str, query_embedding: list[float], limit: int = 5) -> list[dict]:
    from extractor import surreal_db

    """Find the closest unexpired user memories using the HNSW index via the KNN operator."""
    knn_k = min(max(int(limit), 1) * 3, 50)
    # knn_k is a bounded integer derived from limit; never user-controlled text.
    sql = (
        "SELECT memory_text, "  # nosec B608 # noqa: S608 -- knn_k is a bounded integer; values are bound params
        "vector::distance::knn() AS score "
        "FROM user_memories "
        "WHERE user_id = $user_id AND embedding != NONE "
        "AND expires_at != NONE AND expires_at > time::now() "
        f"AND embedding <|{knn_k},40|> $query_embedding "
        "ORDER BY score ASC "
        "LIMIT $limit;"
    )
    return surreal_db._first_result(
        surreal_db._run(
            sql,
            {
                "user_id": user_id,
                "query_embedding": query_embedding,
                "limit": limit,
            },
        )
    )


def count_user_memories(user_id: str) -> int:
    from extractor import surreal_db

    """Count unexpired user memories in SurrealDB."""
    sql = (
        "SELECT count() AS n FROM user_memories "
        "WHERE user_id = $user_id AND expires_at != NONE AND expires_at > time::now() GROUP ALL;"
    )
    results = surreal_db._first_result(surreal_db._run(sql, {"user_id": user_id}))
    if results:
        return results[0].get("n", 0)
    return 0


def clear_user_memories(user_id: str) -> None:
    from extractor import surreal_db

    """Clear all memories for a user in SurrealDB."""
    sql = "DELETE FROM user_memories WHERE user_id = $user_id;"
    surreal_db._run(sql, {"user_id": user_id})


def purge_expired_user_memories() -> int:
    from extractor import surreal_db

    """Delete expired user memories, including legacy rows with no expires_at."""
    count_sql = "SELECT count() AS n FROM user_memories WHERE expires_at IS NONE OR expires_at < time::now() GROUP ALL;"
    count_rows = surreal_db._first_result(surreal_db._run(count_sql))
    count = count_rows[0].get("n", 0) if count_rows else 0
    surreal_db._run("DELETE FROM user_memories WHERE expires_at IS NONE OR expires_at < time::now();")
    return count
