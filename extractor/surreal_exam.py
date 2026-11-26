# Copyright (c) 2026 KORDA Contributors.
#
# This file is part of KORDA.
#
# KORDA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# KORDA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with KORDA.  If not, see <https://www.gnu.org/licenses/>.


from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from django.conf import settings

# ── KORDA Lane 2: Quiz Studio & FSRS Attempt Persistence ─────────────────────

FIELD_FSRS_DIFFICULTY = "fsrs_difficulty"


def _normalize_exam_item_payload(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "item_id": str(item["item_id"]),
        "topic": str(item.get("topic") or ""),
        "difficulty": str(item.get("difficulty") or "Medium"),
        "item_format": str(item.get("item_format") or "MCQ"),
        "stem": str(item.get("stem") or ""),
        "choices": list(item.get("choices") or []),
        "correct_answer": item.get("correct_answer"),
        "hint": str(item.get("hint") or ""),
        "distractor_rationales": item.get("distractor_rationales") or {},
        "anchor_ids": list(item.get("anchor_ids") or []),
    }


def save_exam_items(owner_id: str, items: list[dict[str, Any]], crystal_ids: list[str] | None = None) -> int:
    from extractor import surreal_db

    """Persist generated exam_item records and RELATE tests->crystal edges in a single transaction."""
    if not items:
        return 0

    normalized = [_normalize_exam_item_payload(item) for item in items]

    if getattr(settings, "SURREALDB_OFFLINE", False):
        for entry in normalized:
            existing = surreal_db._test_korda_exam_items.get(str(entry["item_id"])) or {}
            surreal_db._test_korda_exam_items[str(entry["item_id"])] = {
                **existing,
                **entry,
                "owner_id": str(owner_id),
                "crystal_ids": list(crystal_ids or []),
                "fsrs_stability": float(existing.get("fsrs_stability") or 1.0),
                FIELD_FSRS_DIFFICULTY: float(existing.get(FIELD_FSRS_DIFFICULTY) or 5.0),
            }
        return len(normalized)

    sql = """
    BEGIN TRANSACTION;
    LET $owned_crystals = (SELECT VALUE crystal_id FROM crystal WHERE owner_id = $owner_id AND crystal_id INSIDE $crystal_ids);
    FOR $it IN $items {
        LET $e = type::record("exam_item", $it.item_id);
        UPSERT $e MERGE {
            item_id: $it.item_id,
            owner_id: $owner_id,
            topic: $it.topic,
            difficulty: $it.difficulty,
            item_format: $it.item_format,
            stem: $it.stem,
            choices: $it.choices,
            correct_answer: $it.correct_answer,
            hint: $it.hint,
            distractor_rationales: $it.distractor_rationales,
            anchor_ids: $it.anchor_ids
        };
        FOR $cid IN $owned_crystals {
            LET $c = type::record("crystal", $cid);
            RELATE $e->tests->$c;
        };
    };
    COMMIT TRANSACTION;
    """
    res = surreal_db._run(
        sql,
        {
            "owner_id": str(owner_id),
            "items": normalized,
            "crystal_ids": [str(c) for c in (crystal_ids or [])],
        },
    )
    surreal_db._check_surreal_error(res)
    return len(normalized)


def list_exam_items(
    owner_id: str,
    *,
    difficulty: str | None = None,
    due_only: bool = True,
    limit: int = 50,
) -> list[dict[str, Any]]:
    from extractor import surreal_db

    """Fetch persistent exam items for an owner (zero LLM cost on retakes), prioritizing FSRS due items."""
    k_limit = max(1, min(int(limit), 200))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        now_dt = datetime.now(UTC)
        all_rows = [
            it
            for it in surreal_db._test_korda_exam_items.values()
            if it.get("owner_id") == str(owner_id)
            and (not difficulty or difficulty not in {"Easy", "Medium", "Hard"} or it.get("difficulty") == difficulty)
        ]
        if due_only:
            due_rows = [
                it
                for it in all_rows
                if it.get("fsrs_due_at") is None
                or (isinstance(it.get("fsrs_due_at"), datetime) and it["fsrs_due_at"] <= now_dt)
            ]
            if due_rows:
                return due_rows[:k_limit]
        return all_rows[:k_limit]
    params: dict[str, Any] = {"owner_id": str(owner_id), "limit": k_limit}
    diff_clause = ""
    if difficulty and difficulty in {"Easy", "Medium", "Hard"}:
        diff_clause = " AND difficulty = $difficulty"
        params["difficulty"] = difficulty
    due_clause = " AND (fsrs_due_at == NONE OR fsrs_due_at <= time::now())" if due_only else ""
    sql = (
        f"SELECT * FROM exam_item WHERE owner_id = $owner_id{diff_clause}{due_clause} "  # nosec B608 # noqa: S608 -- static clauses
        "ORDER BY fsrs_due_at ASC LIMIT $limit;"
    )
    rows = surreal_db._first_result(surreal_db._run(sql, params))
    if not rows and due_only:
        fallback_sql = (
            f"SELECT * FROM exam_item WHERE owner_id = $owner_id{diff_clause} "  # nosec B608 # noqa: S608 -- static clause
            "ORDER BY fsrs_due_at ASC LIMIT $limit;"
        )
        rows = surreal_db._first_result(surreal_db._run(fallback_sql, params))
    return rows


def get_exam_items_by_ids(owner_id: str, item_ids: list[str]) -> list[dict[str, Any]]:
    from extractor import surreal_db

    """Fetch specific owner-scoped exam items by ID for session-accurate grading."""
    if not item_ids:
        return []
    wanted = {str(i) for i in item_ids}
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return [
            it
            for it in surreal_db._test_korda_exam_items.values()
            if it.get("owner_id") == str(owner_id) and str(it.get("item_id")) in wanted
        ]
    sql = "SELECT * FROM exam_item WHERE owner_id = $owner_id AND item_id INSIDE $item_ids;"
    return surreal_db._first_result(surreal_db._run(sql, {"owner_id": str(owner_id), "item_ids": list(wanted)}))


def _apply_offline_fsrs(
    item: dict[str, Any],
    *,
    is_correct: bool,
    rating: int,
    base_stability: float | None = None,
    base_difficulty: float | None = None,
    increment_reps: bool = True,
) -> None:
    from datetime import timedelta

    stability = float(base_stability if base_stability is not None else (item.get("fsrs_stability") or 1.0))
    difficulty = float(base_difficulty if base_difficulty is not None else (item.get(FIELD_FSRS_DIFFICULTY) or 5.0))
    passed = bool(is_correct) and rating >= 2
    next_stability = min(stability * (1.2 + (rating * 0.35)), 365.0) if passed else max(stability * 0.5, 0.5)
    next_difficulty = max(difficulty - 0.3, 1.0) if passed else min(difficulty + 0.8, 10.0)
    interval_days = max(round(next_stability), 1)
    item["fsrs_stability"] = next_stability
    item[FIELD_FSRS_DIFFICULTY] = next_difficulty
    if increment_reps:
        item["fsrs_reps"] = int(item.get("fsrs_reps") or 0) + 1
    item["fsrs_due_at"] = datetime.now(UTC) + timedelta(days=interval_days)


def record_attempt(
    *,
    attempt_id: str,
    session_id: str,
    item_id: str,
    owner_id: str,
    is_correct: bool,
    rating: int = 3,
    response_ms: int = 0,
) -> bool:
    from extractor import surreal_db

    """Record an exam attempt idempotently; SurrealDB evt_attempt_fsrs_update updates FSRS automatically."""
    safe_rating = max(1, min(int(rating), 4))
    safe_ms = max(0, int(response_ms))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        if session_id and any(
            a.get("session_id") == session_id and a.get("item_id") == item_id and a.get("owner_id") == str(owner_id)
            for a in surreal_db._test_korda_attempts
        ):
            return False
        item = surreal_db._test_korda_exam_items.get(str(item_id))
        pre_stab = float(item.get("fsrs_stability") or 1.0) if item else 1.0
        pre_diff = float(item.get(FIELD_FSRS_DIFFICULTY) or 5.0) if item else 5.0
        surreal_db._test_korda_attempts.append(
            {
                "attempt_id": attempt_id,
                "session_id": session_id,
                "item_id": item_id,
                "owner_id": str(owner_id),
                "is_correct": bool(is_correct),
                "rating": safe_rating,
                "response_ms": safe_ms,
                "pre_stability": pre_stab,
                "pre_difficulty": pre_diff,
            }
        )
        if item and item.get("owner_id") == str(owner_id):
            _apply_offline_fsrs(item, is_correct=bool(is_correct), rating=safe_rating)
        return True
    sql = """
    BEGIN TRANSACTION;
    LET $owned_item = (SELECT fsrs_stability, fsrs_difficulty FROM exam_item WHERE item_id = $item_id AND owner_id = $owner_id LIMIT 1)[0];
    LET $existing = (SELECT VALUE attempt_id FROM attempt WHERE session_id = $session_id AND item_id = $item_id AND owner_id = $owner_id LIMIT 1)[0];
    IF $owned_item != NONE AND $existing == NONE {
        CREATE type::record("attempt", $attempt_id) CONTENT {
            attempt_id: $attempt_id,
            session_id: $session_id,
            item_id: $item_id,
            owner_id: $owner_id,
            is_correct: $is_correct,
            rating: $rating,
            response_ms: $response_ms,
            pre_stability: $owned_item.fsrs_stability,
            pre_difficulty: $owned_item.fsrs_difficulty
        };
        LET $a = type::record("attempt", $attempt_id);
        LET $e = type::record("exam_item", $item_id);
        RELATE $a->attempted_on->$e;
    };
    RETURN $owned_item != NONE AND $existing == NONE;
    COMMIT TRANSACTION;
    """
    res = surreal_db._run(
        sql,
        {
            "attempt_id": attempt_id,
            "session_id": session_id,
            "item_id": item_id,
            "owner_id": str(owner_id),
            "is_correct": bool(is_correct),
            "rating": safe_rating,
            "response_ms": safe_ms,
        },
    )
    surreal_db._check_surreal_error(res)
    return bool(surreal_db._last_non_null_result(res))


def update_attempt_rating(*, session_id: str, item_id: str, owner_id: str, rating: int) -> bool:
    from extractor import surreal_db

    """Update an existing session attempt's FSRS self-rating (1=Again..4=Easy)."""
    safe_rating = max(1, min(int(rating), 4))
    if getattr(settings, "SURREALDB_OFFLINE", False):
        for att in surreal_db._test_korda_attempts:
            if (
                att.get("session_id") == session_id
                and att.get("item_id") == item_id
                and att.get("owner_id") == str(owner_id)
            ):
                att["rating"] = safe_rating
                item = surreal_db._test_korda_exam_items.get(str(item_id))
                if item and item.get("owner_id") == str(owner_id):
                    _apply_offline_fsrs(
                        item,
                        is_correct=bool(att.get("is_correct")),
                        rating=safe_rating,
                        base_stability=att.get("pre_stability"),
                        base_difficulty=att.get("pre_difficulty"),
                        increment_reps=False,
                    )
                return True
        return False
    sql = """
    UPDATE attempt SET rating = $rating
    WHERE session_id = $session_id AND item_id = $item_id AND owner_id = $owner_id;
    """
    res = surreal_db._run(
        sql,
        {
            "session_id": session_id,
            "item_id": item_id,
            "owner_id": str(owner_id),
            "rating": safe_rating,
        },
    )
    surreal_db._check_surreal_error(res)
    return bool(surreal_db._first_result(res))


def _offline_get_anchors_by_ids(anchor_ids: list[str], owner_id: str) -> dict[str, dict[str, Any]]:
    from extractor import surreal_db

    anchors_map: dict[str, dict[str, Any]] = {}
    for aid in anchor_ids:
        anc = surreal_db._test_korda_anchors.get(str(aid))
        if not anc or anc.get("owner_id") != str(owner_id):
            continue
        enriched = dict(anc)
        src = surreal_db._test_korda_sources.get(str(anc.get("source_id") or ""))
        if src:
            enriched.setdefault("gcs_uri", src.get("gcs_uri", ""))
            enriched.setdefault("gcs_status", src.get("gcs_status", "active"))
            enriched.setdefault("sha256", src.get("sha256", ""))
            enriched.setdefault("source_title", src.get("title", ""))
        anchors_map[str(aid)] = enriched
    return anchors_map


def get_anchors_by_ids(anchor_ids: list[str], owner_id: str) -> dict[str, dict[str, Any]]:
    from extractor import surreal_db

    """Fetch anchor evidence records enriched with parent source GCS/Tombstone metadata."""
    if not anchor_ids:
        return {}
    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _offline_get_anchors_by_ids(anchor_ids, owner_id)
    sql = "SELECT * FROM anchor WHERE owner_id = $owner_id AND anchor_id INSIDE $anchor_ids;"
    rows = surreal_db._first_result(surreal_db._run(sql, {"owner_id": str(owner_id), "anchor_ids": list(anchor_ids)}))
    anchors_map = {str(r["anchor_id"]): r for r in rows if isinstance(r, dict) and r.get("anchor_id")}
    source_ids = list({str(r.get("source_id")) for r in anchors_map.values() if r.get("source_id")})
    if not source_ids:
        return anchors_map
    src_sql = (
        "SELECT source_id, gcs_uri, gcs_status, sha256, title AS source_title "
        "FROM source WHERE owner_id = $owner_id AND source_id INSIDE $source_ids;"
    )
    src_rows = surreal_db._first_result(surreal_db._run(src_sql, {"owner_id": str(owner_id), "source_ids": source_ids}))
    src_map = {str(s["source_id"]): s for s in src_rows if isinstance(s, dict) and s.get("source_id")}
    for anc in anchors_map.values():
        src = src_map.get(str(anc.get("source_id") or ""))
        if src:
            anc.setdefault("gcs_uri", src.get("gcs_uri", ""))
            anc.setdefault("gcs_status", src.get("gcs_status", "active"))
            anc.setdefault("sha256", src.get("sha256", ""))
            anc.setdefault("source_title", src.get("source_title", ""))
    return anchors_map
