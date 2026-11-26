"""
KORDA SurrealDB Provenance & Anchor Retrieval Adapter

Lane 1 functions split out of ``surreal_db.py``: source versioning,
anchor persistence with provenance, hybrid (BM25 + HNSW RRF) anchor
search, tombstoning, and crystal creation/listing. Infrastructure
(``_run``, result helpers, offline ``_test_korda_*`` stores) is resolved
through ``extractor.surreal_db`` at call time to preserve mock.patch
semantics.
"""

from __future__ import annotations

import re
from typing import Any

from django.conf import settings

_ANCHOR_ID_SAFE_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,200}$")

# ── KORDA Lane 1: Provenance Graph & Hybrid Anchor Retrieval ─────────────────


def get_next_source_version(owner_id: str, doc_slug: str) -> int:
    from extractor import surreal_db

    """Return the next monotonic integer version for an owner's doc_slug."""
    if getattr(settings, "SURREALDB_OFFLINE", False):
        versions = [
            int(src.get("version") or 1)
            for src in surreal_db._test_korda_sources.values()
            if src.get("owner_id") == str(owner_id) and src.get("doc_slug") == doc_slug
        ]
        return (max(versions) + 1) if versions else 1
    sql = (
        "SELECT math::max(version) AS max_ver FROM source "
        "WHERE owner_id = $owner_id AND doc_slug = $doc_slug GROUP ALL;"
    )
    rows = surreal_db._first_result(surreal_db._run(sql, {"owner_id": str(owner_id), "doc_slug": doc_slug}))
    if rows and isinstance(rows[0], dict) and rows[0].get("max_ver") is not None:
        return int(rows[0]["max_ver"]) + 1
    return 1


def upsert_source(
    *,
    source_id: str,
    doc_slug: str,
    owner_id: str,
    title: str,
    mime_type: str,
    gcs_uri: str,
    sha256: str,
    version: int = 1,
    custom_time: str | None = None,
) -> dict[str, Any]:
    from extractor import surreal_db

    """Create or update a canonical owner-scoped source node in SurrealDB."""
    if getattr(settings, "SURREALDB_OFFLINE", False):
        existing = surreal_db._test_korda_sources.get(source_id)
        if existing and existing.get("owner_id") != str(owner_id):
            raise PermissionError("Source record belongs to a different owner.")
        rec = {
            "source_id": source_id,
            "doc_slug": doc_slug,
            "owner_id": str(owner_id),
            "title": title,
            "mime_type": mime_type,
            "gcs_uri": gcs_uri,
            "gcs_status": "active",
            "sha256": sha256,
            "version": max(int(version), int(existing.get("version") or 1)) if existing else int(version),
            "status": "active",
            "custom_time": custom_time,
        }
        surreal_db._test_korda_sources[source_id] = rec
        return rec

    sql = """
    BEGIN TRANSACTION;
    LET $s = type::record("source", $source_id);
    LET $existing = (SELECT owner_id, version FROM $s)[0];
    IF $existing != NONE AND $existing.owner_id != $owner_id {
        THROW "Source record belongs to a different owner.";
    };
    LET $next_ver = IF $existing != NONE AND $existing.version > $version THEN $existing.version ELSE $version END;
    LET $saved = (UPSERT $s CONTENT {
        source_id: $source_id,
        doc_slug: $doc_slug,
        owner_id: $owner_id,
        title: $title,
        mime_type: $mime_type,
        gcs_uri: $gcs_uri,
        gcs_status: "active",
        sha256: $sha256,
        version: $next_ver,
        status: "active",
        custom_time: IF $custom_time != NONE AND $custom_time != NULL THEN type::datetime($custom_time) ELSE NONE END,
        updated_at: time::now()
    });
    RETURN $saved[0];
    COMMIT TRANSACTION;
    """
    res = surreal_db._run(
        sql,
        {
            "source_id": source_id,
            "doc_slug": doc_slug,
            "owner_id": str(owner_id),
            "title": title,
            "mime_type": mime_type,
            "gcs_uri": gcs_uri,
            "sha256": sha256,
            "version": int(version),
            "custom_time": custom_time,
        },
    )
    surreal_db._check_surreal_error(res)
    row = surreal_db._last_non_null_result(res)
    return row if isinstance(row, dict) else {"source_id": source_id}


def _resolve_anchor_id(source_id: str, ver: int, idx: int, raw: Any, seen_ids: set[str]) -> str:
    """Return a globally unique, record-id-safe anchor id for this batch slot.

    anchor_id doubles as the SurrealDB record id and the lookup key used by
    crystals/exam items. Model-supplied ids are namespaced under this source;
    anything unsafe or duplicated within the batch falls back to the
    deterministic default.
    """
    raw_aid = str(raw or "").strip()[:200]
    if raw_aid and not raw_aid.startswith(f"{source_id}:"):
        raw_aid = f"{source_id}:{raw_aid}"
    aid = raw_aid if raw_aid and _ANCHOR_ID_SAFE_RE.fullmatch(raw_aid) else ""
    if not aid or aid in seen_ids:
        aid = f"{source_id}:v{ver}:{idx}"
    seen_ids.add(aid)
    return aid


def _build_anchor_payloads(source_id: str, ver: int, anchors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    payload_anchors = []
    seen_ids: set[str] = set()
    for idx, anc in enumerate(anchors):
        item: dict[str, Any] = {
            "anchor_id": _resolve_anchor_id(source_id, ver, idx, anc.get("anchor_id"), seen_ids),
            "quote": str(anc.get("quote") or ""),
        }
        for opt_key in ("page_num", "section_" + "heading", "start_sec", "end_sec", "figure_id", "embedding"):
            if anc.get(opt_key) is not None:
                item[opt_key] = anc[opt_key]
        payload_anchors.append(item)
    return payload_anchors


def _persist_offline_anchors(
    source_id: str, doc_slug: str, owner_id: str, ver: int, payload_anchors: list[dict[str, Any]]
) -> int:
    from extractor import surreal_db

    if ver > 1:
        for existing_anc in surreal_db._test_korda_anchors.values():
            if (
                existing_anc.get("owner_id") == str(owner_id)
                and existing_anc.get("doc_slug") == doc_slug
                and int(existing_anc.get("version") or 1) < ver
                and existing_anc.get("status") == "active"
            ):
                existing_anc["status"] = "superseded"
    for item in payload_anchors:
        surreal_db._test_korda_anchors[item["anchor_id"]] = {
            **item,
            "source_id": source_id,
            "doc_slug": doc_slug,
            "owner_id": str(owner_id),
            "version": ver,
            "status": "active",
        }
    return len(payload_anchors)


def create_anchors_with_provenance(
    *,
    source_id: str,
    doc_slug: str,
    owner_id: str,
    version: int = 1,
    anchors: list[dict[str, Any]],
) -> int:
    from extractor import surreal_db

    """Persist verbatim anchors and RELATE anchored_in edges inside a batched SurrealDB transaction."""
    if not anchors:
        return 0

    ver = max(1, int(version))
    payload_anchors = _build_anchor_payloads(source_id, ver, anchors)

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _persist_offline_anchors(source_id, doc_slug, owner_id, ver, payload_anchors)

    sql = """
    BEGIN TRANSACTION;
    LET $s = type::record("source", $source_id);
    FOR $anc IN $anchors {
        LET $a = type::record("anchor", $anc.anchor_id);
        CREATE $a CONTENT {
            anchor_id: $anc.anchor_id,
            source_id: $source_id,
            doc_slug: $doc_slug,
            owner_id: $owner_id,
            version: $version,
            status: "active",
            quote: $anc.quote,
            page_num: IF $anc.page_num != NONE AND $anc.page_num != NULL THEN $anc.page_num ELSE NONE END,
            section_heading: IF $anc.section_heading != NONE AND $anc.section_heading != NULL THEN $anc.section_heading ELSE NONE END,
            start_sec: IF $anc.start_sec != NONE AND $anc.start_sec != NULL THEN $anc.start_sec ELSE NONE END,
            end_sec: IF $anc.end_sec != NONE AND $anc.end_sec != NULL THEN $anc.end_sec ELSE NONE END,
            figure_id: IF $anc.figure_id != NONE AND $anc.figure_id != NULL THEN $anc.figure_id ELSE NONE END,
            embedding: IF $anc.embedding != NONE AND $anc.embedding != NULL THEN $anc.embedding ELSE NONE END
        };
        RELATE $a->anchored_in->$s;
    };
    COMMIT TRANSACTION;
    """
    res = surreal_db._run(
        sql,
        {
            "source_id": source_id,
            "doc_slug": doc_slug,
            "owner_id": str(owner_id),
            "version": ver,
            "anchors": payload_anchors,
        },
    )
    surreal_db._check_surreal_error(res)
    return len(payload_anchors)


def _offline_search_anchors(query_text: str, owner_id: str, limit: int) -> list[dict[str, Any]]:
    from extractor import surreal_db

    terms = [t for t in query_text.lower().split() if t]
    matches: list[dict[str, Any]] = []
    for anc in surreal_db._test_korda_anchors.values():
        if anc.get("owner_id") != str(owner_id) or anc.get("status") != "active":
            continue
        quote_lower = str(anc.get("quote") or "").lower()
        score = sum(1 for t in terms if t in quote_lower) if terms else 1
        if score > 0:
            matches.append({**anc, "rrf_score": float(score)})
    matches.sort(key=lambda r: r["rrf_score"], reverse=True)
    return matches[:limit]


def search_anchors_hybrid(
    query_text: str,
    *,
    owner_id: str,
    query_embedding: list[float] | None = None,
    limit: int = 12,
    doc_slug: str | None = None,
) -> list[dict[str, Any]]:
    from extractor import surreal_db

    """Run BM25 + F16 HNSW search on active anchors and fuse via Reciprocal Rank Fusion (RRF)."""
    k_limit = max(1, min(int(limit), 50))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        matches = _offline_search_anchors(query_text, owner_id, k_limit)
        if doc_slug:
            matches = [m for m in matches if m.get("doc_slug") == doc_slug]
        return matches[:k_limit]

    doc_filter = " AND doc_slug = $doc_slug" if doc_slug else ""
    params: dict[str, Any] = {"owner_id": str(owner_id), "limit": k_limit}
    if doc_slug:
        params["doc_slug"] = doc_slug

    bm25_rows: list[dict[str, Any]] = []
    if query_text.strip():
        bm25_sql = f"""
        SELECT anchor_id, source_id, doc_slug, quote, page_num, section_heading, start_sec, end_sec, figure_id,
               search::score(1) AS bm25_score, search::highlight('**', '**', 1) AS highlighted_quote
        FROM anchor
        WHERE owner_id = $owner_id AND status = "active"{doc_filter} AND quote @1@ $query_text
        ORDER BY bm25_score DESC
        LIMIT $limit;
        """  # nosec B608 # noqa: S608 -- static doc_filter clause
        bm25_rows = surreal_db._first_result(surreal_db._run(bm25_sql, {**params, "query_text": query_text}))

    hnsw_rows: list[dict[str, Any]] = []
    if query_embedding:
        hnsw_k = min(k_limit * 3, 100)
        hnsw_sql = f"""
        SELECT anchor_id, source_id, doc_slug, quote, page_num, section_heading, start_sec, end_sec, figure_id,
               vector::distance::knn() AS dist
        FROM anchor
        WHERE owner_id = $owner_id AND status = "active"{doc_filter} AND embedding != NONE
              AND embedding <|{hnsw_k},40|> $query_embedding
        ORDER BY dist ASC
        LIMIT $limit;
        """  # nosec B608 # noqa: S608 -- hnsw_k is a bounded integer and doc_filter is static
        hnsw_rows = surreal_db._first_result(surreal_db._run(hnsw_sql, {**params, "query_embedding": query_embedding}))

    rrf_scores: dict[str, float] = {}
    records: dict[str, dict[str, Any]] = {}
    for rank, row in enumerate(bm25_rows, 1):
        aid = str(row.get("anchor_id") or "")
        if not aid:
            continue
        rrf_scores[aid] = rrf_scores.get(aid, 0.0) + (1.0 / (60.0 + rank))
        records[aid] = row
    for rank, row in enumerate(hnsw_rows, 1):
        aid = str(row.get("anchor_id") or "")
        if not aid:
            continue
        rrf_scores[aid] = rrf_scores.get(aid, 0.0) + (1.0 / (60.0 + rank))
        records.setdefault(aid, row)

    ranked_ids = sorted(rrf_scores, key=lambda aid: rrf_scores[aid], reverse=True)[:k_limit]
    return [{**records[aid], "rrf_score": rrf_scores[aid]} for aid in ranked_ids]


def tombstone_source(source_id: str, owner_id: str) -> bool:
    from extractor import surreal_db

    """Mark a purged GCS asset as a Tombstone while preserving sha256 and verbatim anchors."""
    if getattr(settings, "SURREALDB_OFFLINE", False):
        src = surreal_db._test_korda_sources.get(source_id)
        if src and src.get("owner_id") == str(owner_id):
            src["gcs_status"] = "purged"
            src["gcs_uri"] = ""
            return True
        return False
    sql = """
    UPDATE source SET gcs_status = "purged", gcs_uri = "", updated_at = time::now()
    WHERE source_id = $source_id AND owner_id = $owner_id;
    """
    res = surreal_db._run(sql, {"source_id": source_id, "owner_id": str(owner_id)})
    surreal_db._check_surreal_error(res)
    rows = surreal_db._first_result(res)
    return bool(rows)


def create_crystal_with_provenance(
    *,
    crystal_id: str,
    owner_id: str,
    doc_slug: str,
    title: str,
    summary: str,
    anchor_ids: list[str],
    status: str = "verified",
    tags: list[str] | None = None,
) -> dict[str, Any]:
    from extractor import surreal_db

    """Create a crystal record and RELATE grounded_in edges to owner-verified supporting anchors."""
    dedup_aids = list(dict.fromkeys(str(a) for a in (anchor_ids or []) if a))
    safe_status = status if status in {"draft", "verified", "archived"} else "draft"
    if getattr(settings, "SURREALDB_OFFLINE", False):
        owned_aids = [
            aid for aid in dedup_aids if surreal_db._test_korda_anchors.get(aid, {}).get("owner_id") == str(owner_id)
        ]
        effective_status = safe_status if (safe_status != "verified" or owned_aids) else "draft"
        rec = {
            "crystal_id": crystal_id,
            "owner_id": str(owner_id),
            "doc_slug": doc_slug,
            "title": title,
            "summary": summary,
            "status": effective_status,
            "tags": list(tags or []),
            "anchor_ids": owned_aids,
        }
        surreal_db._test_korda_crystals[crystal_id] = rec
        return rec
    sql = """
    BEGIN TRANSACTION;
    LET $owned_anchors = (SELECT VALUE anchor_id FROM anchor WHERE owner_id = $owner_id AND anchor_id INSIDE $anchor_ids);
    LET $effective_status = IF $status == "verified" AND array::len($owned_anchors) == 0 THEN "draft" ELSE $status END;
    LET $c = type::record("crystal", $crystal_id);
    LET $created = (UPSERT $c CONTENT {
        crystal_id: $crystal_id,
        owner_id: $owner_id,
        doc_slug: $doc_slug,
        title: $title,
        summary: $summary,
        status: $effective_status,
        tags: $tags,
        updated_at: time::now()
    });
    FOR $aid IN $owned_anchors {
        LET $a = type::record("anchor", $aid);
        RELATE $c->grounded_in->$a;
    };
    RETURN $created[0];
    COMMIT TRANSACTION;
    """
    res = surreal_db._run(
        sql,
        {
            "crystal_id": crystal_id,
            "owner_id": str(owner_id),
            "doc_slug": doc_slug,
            "title": title,
            "summary": summary,
            "status": safe_status,
            "tags": list(tags or []),
            "anchor_ids": dedup_aids,
        },
    )
    surreal_db._check_surreal_error(res)
    last = surreal_db._last_non_null_result(res)
    if isinstance(last, dict):
        return last
    raise RuntimeError(f"Failed to persist crystal {crystal_id}")


def list_crystals_for_user(
    owner_id: str,
    *,
    doc_slug: str | None = None,
    status: str = "verified",
    limit: int = 100,
) -> list[dict[str, Any]]:
    from extractor import surreal_db

    """Fetch user-owned crystals along with their grounded anchor_ids."""
    k_limit = max(1, min(int(limit), 500))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        rows = [
            c
            for c in surreal_db._test_korda_crystals.values()
            if c.get("owner_id") == str(owner_id)
            and c.get("status") == status
            and (not doc_slug or c.get("doc_slug") == doc_slug)
        ]
        return rows[:k_limit]
    params: dict[str, Any] = {
        "owner_id": str(owner_id),
        "status": status,
        "limit": k_limit,
    }
    slug_clause = ""
    if doc_slug:
        slug_clause = " AND doc_slug = $doc_slug"
        params["doc_slug"] = doc_slug
    sql = (
        f"SELECT *, ->grounded_in->anchor.anchor_id AS anchor_ids "  # nosec B608 # noqa: S608 -- static clause
        f"FROM crystal WHERE owner_id = $owner_id AND status = $status{slug_clause} "
        "ORDER BY updated_at DESC LIMIT $limit;"
    )
    return surreal_db._first_result(surreal_db._run(sql, params))


def list_active_gcs_sources(limit: int = 100) -> list[dict[str, Any]]:
    """List sources still marked active with a GCS asset, for asset reconciliation."""
    from extractor import surreal_db

    k_limit = max(1, min(int(limit), 500))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [
            {
                "source_id": src.get("source_id"),
                "owner_id": src.get("owner_id"),
                "doc_slug": src.get("doc_slug"),
                "gcs_uri": src.get("gcs_uri", ""),
            }
            for src in surreal_db._test_korda_sources.values()
            if src.get("gcs_status", "active") == "active" and src.get("gcs_uri")
        ][:k_limit]
    sql = (
        "SELECT source_id, owner_id, doc_slug, gcs_uri FROM source "
        "WHERE gcs_status = 'active' AND gcs_uri != '' "
        "ORDER BY updated_at ASC LIMIT $limit;"
    )
    rows = surreal_db._first_result(surreal_db._run(sql, {"limit": k_limit}))
    return [r for r in rows if isinstance(r, dict) and r.get("source_id")]
