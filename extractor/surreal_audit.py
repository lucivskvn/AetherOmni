"""
KORDA SurrealDB Audit & Spend-Ledger Adapter

Audit log listing/writing and the monthly spend ledger split out of
``surreal_db.py``. Infrastructure resolves through
``extractor.surreal_db`` at call time to preserve mock.patch semantics.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from decimal import Decimal

from django.conf import settings

logger = logging.getLogger(__name__)


def _record_document_spend(doc_uuid: str, spend_month: datetime, cost: float, in_tok: int, out_tok: int) -> bool:
    from extractor import surreal_db

    """Atomically record one deleted document's spend, tolerating retry attempts."""
    sql = (
        "BEGIN TRANSACTION; "
        "LET $existing = (SELECT id FROM document_spend_reconciliations "
        "WHERE doc_uuid = $doc_uuid LIMIT 1); "
        "IF array::len($existing) = 0 { "
        "CREATE document_spend_reconciliations CONTENT { "
        "doc_uuid: $doc_uuid, year: $year, month: $month, cost_usd: $cost_usd, "
        "input_tokens: $input_tokens, output_tokens: $output_tokens "
        "}; }; COMMIT TRANSACTION;"
    )
    try:
        surreal_db._run(
            sql,
            {
                "doc_uuid": doc_uuid,
                "year": spend_month.year,
                "month": spend_month.month,
                "cost_usd": cost,
                "input_tokens": in_tok,
                "output_tokens": out_tok,
            },
        )
    except Exception as exc:
        logger.warning("[Delete] Failed to reconcile document spend: %s", exc)
        return False
    return True


# ── Monthly spend ledger ──────────────────────────────────────────────────────
# IMPORTANT: this ledger tracks NON-DOCUMENT spend only (raw-lane anchor
# extraction, quiz generation, memory distillation, stream/unified responses).
# Document-pipeline spend (OCR/refinement) accumulates on documents.cost_usd and
# is flushed to document_spend_reconciliations on delete — routing document
# work through these helpers would double-count budget spend.


def monthly_spend_ledger_add(year: int, month: int, cost_usd: float, input_tokens: int, output_tokens: int) -> bool:
    from extractor import surreal_db

    """Append one NON-DOCUMENT spend event to the monthly ledger (atomic INSERT, no read-modify-write)."""
    sql = (
        "CREATE monthly_spend_ledger CONTENT {"
        "year: $year, month: $month, cost_usd: $cost_usd, "
        "input_tokens: $input_tokens, output_tokens: $output_tokens};"
    )
    try:
        surreal_db._run(
            sql,
            {
                "year": int(year),
                "month": int(month),
                "cost_usd": float(cost_usd),
                "input_tokens": int(input_tokens or 0),
                "output_tokens": int(output_tokens or 0),
            },
        )
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to append monthly spend ledger entry: %s", exc)
        return False
    return True


def monthly_spend_ledger_total(year: int, month: int) -> dict[str, float] | None:
    from extractor import surreal_db

    """Return summed NON-DOCUMENT ledger totals for a calendar month, or None on failure."""
    sql = (
        "SELECT math::sum(cost_usd) AS cost_usd, math::sum(input_tokens) AS input_tokens, "
        "math::sum(output_tokens) AS output_tokens FROM monthly_spend_ledger "
        "WHERE year = $year AND month = $month GROUP ALL;"
    )
    try:
        rows = surreal_db._first_result(surreal_db._run(sql, {"year": int(year), "month": int(month)}))
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to read monthly spend ledger: %s", exc)
        return None
    if not rows:
        return {"cost_usd": 0.0, "input_tokens": 0.0, "output_tokens": 0.0}
    row = rows[0] if isinstance(rows[0], dict) else {}
    return {
        "cost_usd": float(row.get("cost_usd") or 0.0),
        "input_tokens": float(row.get("input_tokens") or 0.0),
        "output_tokens": float(row.get("output_tokens") or 0.0),
    }


def deleted_document_spend_total(year: int, month: int) -> float | None:
    """Return summed spend of deleted documents for a calendar month.

    Deleted-document spend lives in document_spend_reconciliations so removal
    cannot erase budget consumption. Returns None on failure so callers can
    fail closed rather than under-count the monthly total.
    """
    from extractor import surreal_db

    sql = (
        "SELECT math::sum(cost_usd) AS cost_usd FROM document_spend_reconciliations "
        "WHERE year = $year AND month = $month GROUP ALL;"
    )
    try:
        rows = surreal_db._first_result(surreal_db._run(sql, {"year": int(year), "month": int(month)}))
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to read deleted-document spend: %s", exc)
        return None
    if not rows:
        return 0.0
    row = rows[0] if isinstance(rows[0], dict) else {}
    return float(row.get("cost_usd") or 0.0)


def _flush_document_cost(doc) -> bool:
    """Persist a document's spend before deletion, returning whether deletion is safe."""
    cost = doc.get("cost_usd") or 0.0
    # Attribute spend to the month of last pipeline activity (updated_at),
    # matching the live-spend budget convention (AUD-303): a document created
    # last month but processed this month consumed THIS month's budget, and
    # attributing the flush to created_at would let cross-month deletions
    # escape the current month's total entirely (AUD-394).
    spend_ts = doc.get("updated_at") or doc.get("created_at")
    if cost <= 0:
        return True

    if not spend_ts:
        logger.warning("[SurrealDB] event=surrealdb_missing_created_at")
        return False

    from django.utils.dateparse import parse_datetime as django_parse

    if isinstance(spend_ts, datetime):
        spend_at = spend_ts
    elif isinstance(spend_ts, str):
        spend_at = django_parse(spend_ts)
    else:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=%s", type(spend_ts).__name__)
        return False

    if not spend_at:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=string")
        return False

    input_tokens = doc.get("input_tokens") or 0
    output_tokens = doc.get("output_tokens") or 0
    if not getattr(settings, "SURREALDB_OFFLINE", False):
        doc_uuid = str(doc.get("doc_uuid") or "")
        if not doc_uuid:
            logger.warning("[SurrealDB] event=surrealdb_missing_document_uuid")
            return False
        return _record_document_spend(doc_uuid, spend_at, float(cost), input_tokens, output_tokens)

    from extractor.models import MonthlySpendLog

    try:
        return MonthlySpendLog.add_cost(
            date=spend_at,
            cost=Decimal(str(cost)),
            in_tok=input_tokens,
            out_tok=output_tokens,
        )
    except Exception as exc:
        logger.warning("[Delete] Failed to flush cost to MonthlySpendLog in delete_document: %s", exc)
        return False


def log_audit(
    action: str, user_id: str, doc_uuid: str | None = None, metadata: dict | None = None, ip_address: str = ""
) -> None:
    from extractor import surreal_db

    """Write an audit log entry to SurrealDB."""
    if getattr(settings, "SURREALDB_OFFLINE", False):
        # Offline audit persistence is handled by log_audit_event's Django ORM
        # leg; writing here would only produce swallowed failures (AUD-371).
        return
    params = {
        "action": action,
        "user_id": str(user_id),
        "doc_uuid": str(doc_uuid) if doc_uuid is not None else None,
        "metadata": json.dumps(metadata or {}),
        "ip_address": ip_address,
    }
    sql = (
        "INSERT INTO audit_logs {"
        "  action: $action,"
        "  user_id: $user_id,"
        "  doc_uuid: $doc_uuid,"
        "  metadata: $metadata,"
        "  ip_address: $ip_address"
        "};"
    )
    try:
        surreal_db._run(sql, params)
    except Exception as exc:
        logger.warning("[SurrealDB] Failed to write audit log for action=%s user=%s: %s", action, user_id, exc)
