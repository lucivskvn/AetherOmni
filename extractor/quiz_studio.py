"""KORDA Lane 2: Persistent Quiz Studio & Distraction-Free Mock Exam Engine."""

from __future__ import annotations

import json
import random
import uuid
from typing import Any

from extractor import compiler, llm_gateway, surreal_db

SUPPORTED_FORMATS = ("MCQ", "Multi-Select", "Short Answer", "Fill-in-the-Blank")
SUPPORTED_DIFFICULTIES = ("Easy", "Medium", "Hard")
KEY_DISTRACTOR_RATIONALES = "distractor_rationales"


def _normalize_generated_item(
    raw: dict[str, Any],
    *,
    owner_id: str,
    diff: str,
    default_format: str,
    fallback_anchor_ids: list[str],
) -> dict[str, Any]:
    anchor_ids = [str(a) for a in (raw.get("anchor_ids") or fallback_anchor_ids[:1]) if a]
    return {
        "item_id": uuid.uuid4().hex[:16],
        "owner_id": str(owner_id),
        "topic": str(raw.get("topic") or "General"),
        "difficulty": str(raw.get("difficulty") or diff),
        "item_format": str(raw.get("item_format") or default_format),
        "stem": str(raw.get("stem") or ""),
        "choices": list(raw.get("choices") or []),
        "correct_answer": raw.get("correct_answer"),
        "hint": str(raw.get("hint") or ""),
        KEY_DISTRACTOR_RATIONALES: dict(raw.get(KEY_DISTRACTOR_RATIONALES) or {}),
        "anchor_ids": anchor_ids,
    }


def generate_and_persist_quiz(
    *,
    owner_id: str,
    crystals: list[dict[str, Any]],
    difficulty: str = "Medium",
    count: int = 5,
    formats: list[str] | None = None,
    focus_prompt: str = "",
    blueprint_weights: dict[str, float] | None = None,
) -> list[dict[str, Any]]:
    """Generate persistent quiz items grounded in crystals + anchors (two-tier Gemini routing)."""
    if not crystals:
        return []

    diff = difficulty if difficulty in SUPPORTED_DIFFICULTIES else "Medium"
    fmt_list = [f for f in (formats or ["MCQ"]) if f in SUPPORTED_FORMATS] or ["MCQ"]
    n_items = max(1, min(int(count), 30))

    client = llm_gateway._get_vertex_client()
    if client is None:
        raise RuntimeError(llm_gateway.VAL_ERROR_UNAVAILABLE)

    model_name = llm_gateway.MODEL_TIER_PRO if diff == "Hard" else llm_gateway.MODEL_TIER_LITE
    context_json = json.dumps(crystals[:20], ensure_ascii=False)
    weights_note = f" Blueprint weights: {json.dumps(blueprint_weights)}." if blueprint_weights else ""
    focus_note = f" Focus: {focus_prompt}." if focus_prompt else ""

    prompt = (
        f"Generate {n_items} exam items at difficulty '{diff}' using formats {fmt_list}.{weights_note}{focus_note} "
        "Return strict JSON: "
        '{"items": [{"topic": str, "difficulty": str, "item_format": str, "stem": str, '
        '"choices": [str], "correct_answer": str|list, "hint": str, '
        '"distractor_rationales": {str: str}, "anchor_ids": [str]}]}.\n'
        f"Context crystals:\n{context_json}"
    )

    response = client.models.generate_content(
        model=model_name,
        contents=[prompt],
        config=llm_gateway.types.GenerateContentConfig(
            temperature=0.2,
            response_mime_type=llm_gateway.APPLICATION_JSON,
        ),
    )
    parsed = json.loads(response.text or "{}")
    raw_items = list(parsed.get("items") or [])

    fallback_anchor_ids = [aid for c in crystals for aid in (c.get("anchor_ids") or [])]
    persisted = [
        _normalize_generated_item(
            raw,
            owner_id=owner_id,
            diff=diff,
            default_format=fmt_list[0],
            fallback_anchor_ids=fallback_anchor_ids,
        )
        for raw in raw_items[:n_items]
    ]

    crystal_ids = [str(c["crystal_id"]) for c in crystals if c.get("crystal_id")]
    surreal_db.save_exam_items(owner_id, persisted, crystal_ids=crystal_ids)
    return persisted


def build_mock_exam_session(
    items: list[dict[str, Any]],
    *,
    seed: int | None = None,
    duration_seconds: int = 900,
) -> dict[str, Any]:
    """Build a shuffled Mock Exam payload with answers and rationales stripped until submission."""
    rng = random.Random(seed)  # nosec B311 # noqa: S311 -- non-cryptographic exam question shuffling
    shuffled = list(items)
    rng.shuffle(shuffled)

    questions: list[dict[str, Any]] = []
    for idx, item in enumerate(shuffled, 1):
        choices = list(item.get("choices") or [])
        if choices:
            rng.shuffle(choices)
        questions.append(
            {
                "index": idx,
                "item_id": item.get("item_id"),
                "topic": item.get("topic", ""),
                "difficulty": item.get("difficulty", "Medium"),
                "item_format": item.get("item_format", "MCQ"),
                "stem": item.get("stem", ""),
                "choices": choices,
                "hint": item.get("hint", ""),
            }
        )

    return {
        "session_id": uuid.uuid4().hex[:16],
        "duration_seconds": max(60, int(duration_seconds)),
        "questions": questions,
    }


def _answers_match(expected: Any, submitted: Any, item_format: str) -> bool:
    if item_format == "Multi-Select" and isinstance(expected, list) and isinstance(submitted, list):
        return {str(x).strip().lower() for x in expected} == {str(x).strip().lower() for x in submitted}
    if expected is None or submitted is None:
        return False
    return str(expected).strip().lower() == str(submitted).strip().lower()


def _build_evidence_card(anchor: dict[str, Any]) -> dict[str, Any]:
    is_purged = anchor.get("gcs_status") == "purged" or not anchor.get("gcs_uri")
    return {
        "anchor_id": anchor.get("anchor_id"),
        "source_id": anchor.get("source_id"),
        "coord": compiler.format_anchor_coord(anchor),
        "quote": anchor.get("quote", ""),
        "card_type": "tombstone" if is_purged else "live_gcs",
        "sha256": anchor.get("sha256", ""),
        "signed_url": None if is_purged else anchor.get("signed_url"),
    }


def grade_mock_exam_submission(
    *,
    owner_id: str,
    session_id: str,
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    anchors_by_id: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Grade mock exam answers at zero LLM cost, record attempts, and attach Cited Review cards."""
    anchor_map = anchors_by_id or {}
    results: list[dict[str, Any]] = []
    score = 0

    for item in items:
        item_id = str(item.get("item_id") or "")
        submitted = answers.get(item_id)
        expected = item.get("correct_answer")
        is_correct = _answers_match(expected, submitted, str(item.get("item_format") or "MCQ"))
        if is_correct:
            score += 1

        surreal_db.record_attempt(
            attempt_id=uuid.uuid4().hex[:16],
            session_id=session_id,
            item_id=item_id,
            owner_id=owner_id,
            is_correct=is_correct,
            rating=3 if is_correct else 1,
        )

        evidence = [
            _build_evidence_card(anchor_map[aid]) for aid in (item.get("anchor_ids") or []) if aid in anchor_map
        ]
        results.append(
            {
                "item_id": item_id,
                "stem": item.get("stem", ""),
                "submitted": submitted,
                "correct_answer": expected,
                "is_correct": is_correct,
                KEY_DISTRACTOR_RATIONALES: item.get(KEY_DISTRACTOR_RATIONALES) or {},
                "evidence": evidence,
            }
        )

    return {
        "session_id": session_id,
        "score": score,
        "total": len(items),
        "results": results,
    }


def export_exam_markdown(
    items: list[dict[str, Any]], *, include_answers: bool = True, title: str = "KORDA Mock Exam"
) -> str:
    """Export exam items as a portable Markdown Exam Pack with toggleable Answer Key."""
    lines: list[str] = [f"# {title}", ""]
    for idx, item in enumerate(items, 1):
        lines.append(f"## Question {idx} ({item.get('difficulty', 'Medium')} · {item.get('item_format', 'MCQ')})")
        lines.append("")
        lines.append(str(item.get("stem") or ""))
        lines.append("")
        for opt_idx, choice in enumerate(item.get("choices") or [], 65):
            lines.append(f"- **{chr(opt_idx)}.** {choice}")
        if item.get("choices"):
            lines.append("")
        if item.get("hint"):
            lines.append(f"*Hint:* {item['hint']}")
            lines.append("")

    if include_answers:
        lines.append("## Answer Key & Citations")
        lines.append("")
        for idx, item in enumerate(items, 1):
            anchors = ", ".join(item.get("anchor_ids") or [])
            lines.append(f"- **Q{idx}:** `{item.get('correct_answer')}` (Anchors: {anchors or 'none'})")
        lines.append("")

    return "\n".join(lines)
