"""KORDA Lane 2: Persistent Quiz Studio & Distraction-Free Mock Exam Engine."""

from __future__ import annotations

import json
import logging
import math
import random
import re
import uuid
from typing import Any

from extractor import compiler, llm_gateway, surreal_db

logger = logging.getLogger(__name__)

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


def _sanitize_blueprint_weights(blueprint_weights: dict[str, float] | None) -> dict[str, float]:
    """Filter blueprint_weights to positive finite float entries keyed by string."""
    clean_weights: dict[str, float] = {}
    if not isinstance(blueprint_weights, dict):
        return clean_weights
    for k, v in blueprint_weights.items():
        try:
            w = float(v)
        except TypeError, ValueError:
            continue
        if math.isfinite(w) and w > 0:
            clean_weights[str(k)] = w
    return clean_weights


def _project_crystals_for_prompt(crystals: list[dict[str, Any]]) -> str:
    projected = [
        {
            "crystal_id": str(c.get("crystal_id") or ""),
            "doc_slug": str(c.get("doc_slug") or ""),
            "title": str(c.get("title") or ""),
            "summary": str(c.get("summary") or ""),
            "tags": list(c.get("tags") or []),
            "anchor_ids": list(c.get("anchor_ids") or []),
        }
        for c in crystals[:20]
    ]
    return json.dumps(projected, ensure_ascii=False)


def _record_quiz_generation_spend(model_name: str, response: Any) -> None:
    """Itemize quiz-generation LLM spend into the monthly budget ledger."""
    usage = getattr(response, "usage_metadata", None)
    in_tok, out_tok = llm_gateway.usage_token_counts(usage)
    if not in_tok and not out_tok:
        return
    try:
        from datetime import UTC, datetime

        from extractor.models import MonthlySpendLog

        cost = llm_gateway.calculate_gemini_cost(model_name, in_tok, out_tok)
        if not MonthlySpendLog.add_cost(datetime.now(UTC), cost, in_tok=in_tok, out_tok=out_tok):
            logger.warning("[QuizStudio] Spend ledger write returned False for quiz generation.")
    except Exception as exc:
        logger.warning("[QuizStudio] Failed to record generation spend: %s", exc)


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
    context_json = _project_crystals_for_prompt(crystals)
    clean_weights = _sanitize_blueprint_weights(blueprint_weights)
    weights_note = f" Blueprint weights: {json.dumps(clean_weights)}." if clean_weights else ""
    focus_note = f" Focus: {focus_prompt[:500]}." if focus_prompt else ""

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
    _record_quiz_generation_spend(model_name, response)
    try:
        parsed = json.loads(response.text or "{}")
    except json.JSONDecodeError:
        parsed = {}
    items_field = parsed.get("items") if isinstance(parsed, dict) else []
    raw_items = [it for it in items_field if isinstance(it, dict)] if isinstance(items_field, list) else []

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
        "item_ids": [q["item_id"] for q in questions if q.get("item_id")],
        "questions": questions,
    }


def _norm_answer_text(val: Any) -> str:
    collapsed = re.sub(r"\s+", " ", str(val).strip().lower())
    return collapsed.rstrip(".!?")


def _answers_match(expected: Any, submitted: Any, item_format: str) -> bool:
    if item_format == "Multi-Select" and isinstance(expected, list) and isinstance(submitted, list):
        return {_norm_answer_text(x) for x in expected} == {_norm_answer_text(x) for x in submitted}
    if expected is None or submitted is None:
        return False
    return _norm_answer_text(expected) == _norm_answer_text(submitted)


_STORAGE_CLIENT_CACHE: dict[str, Any] = {}


def _get_storage_client():
    if "client" not in _STORAGE_CLIENT_CACHE:
        from google.cloud import storage  # type: ignore[attr-defined]

        _STORAGE_CLIENT_CACHE["client"] = storage.Client()
    return _STORAGE_CLIENT_CACHE["client"]


def _generate_v4_signed_url(gcs_uri: str) -> str | None:
    """Generate a 15-minute GCS V4 Signed URL for live Cited Review jumps (supports Cloud Run IAM signing)."""
    if not gcs_uri or not gcs_uri.startswith("gs://"):
        return None
    try:
        from datetime import timedelta

        bucket_name, _, blob_name = gcs_uri[5:].partition("/")
        if not bucket_name or not blob_name:
            return None
        client = _get_storage_client()
        blob = client.bucket(bucket_name).blob(blob_name)
        kwargs: dict[str, Any] = {"version": "v4", "expiration": timedelta(minutes=15), "method": "GET"}
        creds = getattr(client, "_credentials", None)
        if creds is not None and not hasattr(creds, "signer"):
            if not getattr(creds, "valid", False) or not getattr(creds, "token", None):
                import google.auth.transport.requests

                creds.refresh(google.auth.transport.requests.Request())
            if getattr(creds, "service_account_email", None) and getattr(creds, "token", None):
                kwargs["service_account_email"] = creds.service_account_email
                kwargs["access_token"] = creds.token
        return blob.generate_signed_url(**kwargs)
    except Exception as exc:
        logger.warning("[QuizStudio] Failed to sign V4 URL for %s: %s", gcs_uri, exc)
        return None


def _build_evidence_card(anchor: dict[str, Any]) -> dict[str, Any]:
    gcs_uri = str(anchor.get("gcs_uri") or "")
    is_purged = anchor.get("gcs_status") == "purged" or not gcs_uri
    signed_url = None if is_purged else (anchor.get("signed_url") or _generate_v4_signed_url(gcs_uri))
    return {
        "anchor_id": anchor.get("anchor_id"),
        "source_id": anchor.get("source_id"),
        "coord": compiler.format_anchor_coord(anchor),
        "quote": anchor.get("quote", ""),
        "card_type": "tombstone" if is_purged else "live_gcs",
        "sha256": anchor.get("sha256", ""),
        "signed_url": signed_url,
    }


def _grade_single_item(
    *,
    owner_id: str,
    session_id: str,
    item: dict[str, Any],
    answers: dict[str, Any],
    rating_map: dict[str, int],
    anchor_map: dict[str, dict[str, Any]],
) -> tuple[bool, dict[str, Any]]:
    item_id = str(item.get("item_id") or "")
    submitted = answers.get(item_id)
    expected = item.get("correct_answer")
    is_correct = _answers_match(expected, submitted, str(item.get("item_format") or "MCQ"))

    raw_rating = rating_map.get(item_id)
    if isinstance(raw_rating, int) and 1 <= raw_rating <= 4:
        item_rating = raw_rating
    else:
        item_rating = 3 if is_correct else 1
    attempt_id = f"{owner_id}:{session_id}:{item_id}" if session_id else uuid.uuid4().hex[:16]
    surreal_db.record_attempt(
        attempt_id=attempt_id,
        session_id=session_id,
        item_id=item_id,
        owner_id=owner_id,
        is_correct=is_correct,
        rating=item_rating,
    )
    evidence = [_build_evidence_card(anchor_map[aid]) for aid in (item.get("anchor_ids") or []) if aid in anchor_map]
    return is_correct, {
        "item_id": item_id,
        "stem": item.get("stem", ""),
        "submitted": submitted,
        "correct_answer": expected,
        "is_correct": is_correct,
        KEY_DISTRACTOR_RATIONALES: item.get(KEY_DISTRACTOR_RATIONALES) or {},
        "evidence": evidence,
    }


def grade_mock_exam_submission(
    *,
    owner_id: str,
    session_id: str,
    items: list[dict[str, Any]],
    answers: dict[str, Any],
    anchors_by_id: dict[str, dict[str, Any]] | None = None,
    ratings: dict[str, int] | None = None,
) -> dict[str, Any]:
    """Grade mock exam answers at zero LLM cost, record attempts idempotently, and attach Cited Review cards."""
    anchor_map = anchors_by_id or {}
    rating_map = ratings or {}
    results: list[dict[str, Any]] = []
    score = 0

    for item in items:
        is_correct, row = _grade_single_item(
            owner_id=owner_id,
            session_id=session_id,
            item=item,
            answers=answers,
            rating_map=rating_map,
            anchor_map=anchor_map,
        )
        if is_correct:
            score += 1
        results.append(row)

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
        lines.extend(
            [
                f"## Question {idx} ({item.get('difficulty', 'Medium')} · {item.get('item_format', 'MCQ')})",
                "",
                str(item.get("stem") or ""),
                "",
            ]
        )
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
