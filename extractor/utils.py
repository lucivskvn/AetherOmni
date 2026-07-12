# Copyright (c) 2026 Knowledge Desk Contributors.
# All rights reserved. Confidential and Proprietary.

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# ── Knative scaling annotation constants (Gap E-18) ───────────────────────────
KNATIVE_MIN_SCALE = "autoscaling.knative.dev/minScale"
KNATIVE_MAX_SCALE = "autoscaling.knative.dev/maxScale"

# ── Re-exports from file_utils.py ─────────────────────────────────────────────
from extractor.file_utils import (
    async_task_with_wakeup,
    calculate_file_sha256,
    clean_html_content,
    format_localized_cost,
    generate_curated_zip_bundle,
    get_client_ip,
    get_google_oidc_token,
    get_locale_currency_details,
    process_csv_local,
    process_txt_local,
    render_markdown_to_html,
)

# ── Re-exports from llm_gateway.py ────────────────────────────────────────────
from extractor.llm_gateway import (
    APPLICATION_JSON,
    GEMINI_API_KEY_ERROR,
    MODEL_GEMINI_FLASH_LITE,
    PREFIX_GOOGLE,
    PROCESS_DOCUMENT_TASK,
    BudgetExceededException,
    GeminiProcessingError,
    UnifiedResponse,
    calculate_gemini_cost,
    calculate_openrouter_cost,
    check_budget_and_api_limit,
    execute_with_backoff,
    generate_llm_content_unified,
    run_stage1_multimodal_ocr,
    run_stage2_editorial_refinement,
)

# ── Re-exports from rag.py ────────────────────────────────────────────────────
from extractor.rag import (
    chunk_document_semantically,
    generate_surreal_embeddings,
    query_semantic_knowledge_rag,
)

# ── Dual-Write Audit Log Helper (Gap E-24) ────────────────────────────────────


def log_audit_event(
    action: str,
    user: Any = None,
    document: Any = None,
    details: str = "",
    ip_address: str | None = None,
    metadata: dict | None = None,
) -> None:
    """
    Dual-write audit log to both SQLite (Django ORM) and SurrealDB.
    Failures in either leg are logged but do not raise to avoid disrupting the main flow.
    """
    from extractor.models import AuditLog

    # Leg 1: Django ORM → SQLite
    try:
        AuditLog.objects.create(
            user=user,
            action=action,
            document=document,
            details=details,
            ip_address=ip_address,
        )
    except Exception as exc:
        logger.warning("[AuditLog] Failed to write SQLite audit entry: %s", exc)

    # Leg 2: SurrealDB audit_logs table
    try:
        from extractor import surreal_db

        user_id = str(user.id) if user else "system"
        doc_uuid = str(document.uuid) if document and hasattr(document, "uuid") else None
        surreal_db.log_audit(
            action=action,
            user_id=user_id,
            doc_uuid=doc_uuid,
            metadata=metadata or {"details": details},
            ip_address=ip_address or "",
        )
    except Exception as exc:
        logger.warning("[AuditLog] Failed to write SurrealDB audit entry: %s", exc)


# ── Supabase Realtime Broadcast Helper (Gap D-7, E-42) ───────────────────────


def validate_url_scheme(url: str) -> None:
    """
    Validates that a URL scheme is secure (strictly https in production, and http/https in debug/test).
    Raises ValueError if validation fails.
    """
    from django.conf import settings

    if not url.startswith(("http://", "https://")):
        raise ValueError("Invalid URL scheme. Only http and https schemes are permitted.")
    if not getattr(settings, "DEBUG", True) and url.startswith("http://"):
        raise ValueError("Insecure URL scheme. Production environments require https.")


def broadcast_status_change(doc_uuid: str, status: str) -> None:
    """
    Broadcast a document status change event over Supabase Realtime.
    Gap E-42: gracefully handles missing/invalid Supabase credentials without crashing.
    This is a fire-and-forget helper — failures are logged only.
    """
    from django.conf import settings

    supabase_url = getattr(settings, "SUPABASE_URL", "")
    supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

    if not supabase_url or not supabase_key:
        # Supabase not configured — silent no-op (local dev or minimal stack)
        return

    try:
        import json
        import urllib.request

        # Use Supabase Realtime HTTP broadcast REST endpoint
        broadcast_url = f"{supabase_url.rstrip('/')}/realtime/v1/api/broadcast"
        validate_url_scheme(broadcast_url)
        payload = json.dumps(
            {
                "messages": [
                    {
                        "topic": "document-updates",
                        "event": "status-changed",
                        "payload": {"uuid": doc_uuid, "status": status},
                    }
                ]
            }
        ).encode("utf-8")

        req = urllib.request.Request(
            broadcast_url,
            data=payload,
            headers={
                "apikey": supabase_key,
                "Authorization": f"Bearer {supabase_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=3) as response:  # nosec B310 nosemgrep
            logger.debug("[Broadcast] Status '%s' sent for doc %s: %s", status, doc_uuid, response.status)
    except Exception as exc:
        # Gap E-42: never crash a task because Supabase is unavailable
        logger.warning("[Broadcast] Failed to broadcast status '%s' for doc %s: %s", status, doc_uuid, exc)


__all__ = [
    "APPLICATION_JSON",
    "GEMINI_API_KEY_ERROR",
    "KNATIVE_MAX_SCALE",
    "KNATIVE_MIN_SCALE",
    "MODEL_GEMINI_FLASH_LITE",
    "PREFIX_GOOGLE",
    "PROCESS_DOCUMENT_TASK",
    "BudgetExceededException",
    "GeminiProcessingError",
    "UnifiedResponse",
    "async_task_with_wakeup",
    "broadcast_status_change",
    "calculate_file_sha256",
    "calculate_gemini_cost",
    "calculate_openrouter_cost",
    "check_budget_and_api_limit",
    "chunk_document_semantically",
    "clean_html_content",
    "execute_with_backoff",
    "format_localized_cost",
    "generate_curated_zip_bundle",
    "generate_llm_content_unified",
    "generate_surreal_embeddings",
    "get_client_ip",
    "get_google_oidc_token",
    "get_locale_currency_details",
    "log_audit_event",
    "process_csv_local",
    "process_txt_local",
    "query_semantic_knowledge_rag",
    "render_markdown_to_html",
    "run_stage1_multimodal_ocr",
    "run_stage2_editorial_refinement",
    "validate_url_scheme",
]
