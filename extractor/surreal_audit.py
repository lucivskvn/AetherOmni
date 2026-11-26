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


def list_audit_logs(limit: int = 100, start: int = 0) -> list[dict]:
    from extractor import surreal_db

    """Retrieve paginated audit logs from SurrealDB ordered by timestamp desc."""

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        qs = AuditLog.objects.all().order_by("-timestamp")[start : start + limit]
        return [surreal_db._audit_log_to_dict(log) for log in qs]

    sql = "SELECT * FROM audit_logs ORDER BY timestamp DESC LIMIT $limit START $start;"
    return surreal_db._first_result(surreal_db._run(sql, {"limit": limit, "start": start}))


def count_audit_logs() -> int:
    from extractor import surreal_db

    """Count total audit logs in SurrealDB."""

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import AuditLog

        return AuditLog.objects.count()

    sql = "SELECT count() AS n FROM audit_logs GROUP ALL;"
    rows = surreal_db._first_result(surreal_db._run(sql))
    return rows[0].get("n", 0) if rows else 0


def _record_document_spend(doc_uuid: str, created_at: datetime, cost: float, in_tok: int, out_tok: int) -> bool:
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
                "year": created_at.year,
                "month": created_at.month,
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


def _flush_document_cost(doc) -> bool:
    """Persist a document's spend before deletion, returning whether deletion is safe."""
    cost = doc.get("cost_usd") or 0.0
    created_at_value = doc.get("created_at")
    if cost <= 0:
        return True

    if not created_at_value:
        logger.warning("[SurrealDB] event=surrealdb_missing_created_at")
        return False

    from django.utils.dateparse import parse_datetime as django_parse

    if isinstance(created_at_value, datetime):
        created_at = created_at_value
    elif isinstance(created_at_value, str):
        created_at = django_parse(created_at_value)
    else:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=%s", type(created_at_value).__name__)
        return False

    if not created_at:
        logger.warning("[SurrealDB] event=surrealdb_invalid_created_at type=string")
        return False

    input_tokens = doc.get("input_tokens") or 0
    output_tokens = doc.get("output_tokens") or 0
    if not getattr(settings, "SURREALDB_OFFLINE", False):
        doc_uuid = str(doc.get("doc_uuid") or "")
        if not doc_uuid:
            logger.warning("[SurrealDB] event=surrealdb_missing_document_uuid")
            return False
        return _record_document_spend(doc_uuid, created_at, float(cost), input_tokens, output_tokens)

    from extractor.models import MonthlySpendLog

    try:
        return MonthlySpendLog.add_cost(
            date=created_at,
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
