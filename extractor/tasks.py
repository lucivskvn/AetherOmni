"""
Async Worker Tasks — AetherOmni v2.0

Pipeline: Stage 1 (Gemini Multimodal OCR) → Stage 2 (Editorial Refinement)
          → Stage 3 (SurrealDB Semantic Chunking + Vector Embeddings)

All task functions receive a `payload` dict (as dispatched by cloud_tasks.enqueue).
The task_handlers.TASK_REGISTRY maps task_name → function.
"""

import logging
import os
import re
import tempfile
import traceback
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone
from django.utils.text import slugify

logger = logging.getLogger(__name__)

_MAX_TITLE_LEN = 255
_MAX_AUTHOR_LEN = 255
_MAX_LANGUAGE_LEN = 100
_MAX_DOCTYPE_LEN = 100
_MAX_SIG_LEN = 255

from extractor import surreal_db
from extractor.models import AuditAction
from extractor.utils import (
    AuditEvent,
    broadcast_status_change,
    check_budget_and_api_limit,
    chunk_document_semantically,
    generate_surreal_embeddings,
    log_audit_event,
    process_csv_local,
    process_excel_local,
    process_json_local,
    process_pdf_local,
    process_txt_local,
    run_stage1_multimodal_ocr,
    run_stage2_editorial_refinement,
)


def format_datetime(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _count_pages_pass1(working_path, chunk_size, overlap, pages_pattern, parent_pattern):
    pages_count = 0
    parent_count = 0
    with open(working_path, "rb") as f:
        buffer = b""
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            content = buffer + chunk
            pages_count += len(pages_pattern.findall(content))
            parent_count += len(parent_pattern.findall(content))
            if len(content) > overlap:
                buffer = content[-overlap:]
            else:
                buffer = content
    return pages_count, parent_count


def _extract_count_from_pages_node(content: bytes, pages_idx: int) -> int | None:
    """Helper to extract /Count digits following a /Type/Pages token."""
    # Strip all standard PDF whitespace characters (ASCII 0, 9, 10, 12, 13, 32)
    type_slice = (
        content[pages_idx : pages_idx + 30]
        .replace(b" ", b"")
        .replace(b"\t", b"")
        .replace(b"\n", b"")
        .replace(b"\r", b"")
        .replace(b"\x0c", b"")
        .replace(b"\x00", b"")
    )
    if not type_slice.startswith(b"/Type/Pages"):
        return None
    count_idx = content.find(b"/Count", pages_idx)
    if count_idx == -1 or count_idx - pages_idx >= 200:
        return None
    after_count = content[count_idx + 6 : count_idx + 30].lstrip()
    digits = []
    for b in after_count:
        if 48 <= b <= 57:
            digits.append(b)
        else:
            break
    return int(bytes(digits)) if digits else None


def _find_pages_count_in_chunk(content: bytes) -> int | None:
    """Scan chunk for /Type/Pages and parse its /Count."""
    idx = 0
    while True:
        pages_idx = content.find(b"/Type", idx)
        if pages_idx == -1:
            return None
        count = _extract_count_from_pages_node(content, pages_idx)
        if count is not None:
            return count
        idx = pages_idx + 5


def _count_pages_pass2(working_path, chunk_size, overlap):
    with open(working_path, "rb") as f:
        buffer = b""
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            content = buffer + chunk
            count = _find_pages_count_in_chunk(content)
            if count is not None:
                return count
            buffer = content[-overlap:] if len(content) > overlap else content
    return 1


def _determine_actual_page_count(working_path: str, doc_type: str) -> int:
    """Helper to parse the PDF file structures and extract page count using regex without loading the entire file into memory."""
    if doc_type != "PDF":
        return 1
    try:
        import re

        chunk_size = 128 * 1024
        overlap = 1024

        pages_pattern = re.compile(rb"/Type\s*/Page\b")
        parent_pattern = re.compile(rb"/Parent\s+\d+\s+\d+\s+R")

        with open(working_path, "rb") as f:
            header = f.read(1024)
            if b"Dummy PDF Content" in header:
                return 1

        pages_count, _parent_count = _count_pages_pass1(
            working_path, chunk_size, overlap, pages_pattern, parent_pattern
        )

        if pages_count > 0:
            return pages_count

        return _count_pages_pass2(working_path, chunk_size, overlap)
    except Exception as e:
        logger.debug("[Worker] Failed to determine real page count for %s: %s", working_path, e)
        return 1


def _truncate(value: str | None, max_len: int) -> str:
    """Safely truncate a string to the maximum database column length."""
    if not value:
        return ""
    return value[:max_len]


def _resolve_user_by_id(uploaded_by_id: Any) -> Any | None:
    """
    Safely resolve a Django User instance from uploaded_by_id, which may be
    a Django integer PK or a Supabase UUID string.
    """
    if not uploaded_by_id:
        return None
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    # Try integer lookup if uploaded_by_id is numeric
    val_str = str(uploaded_by_id).strip()
    if val_str.isdigit():
        return user_model.objects.filter(id=int(val_str)).first()
    return None


def _fail_document(doc_uuid: str, error_message: str, details: str, log_audit: bool = True) -> None:
    """
    Handles pipeline failures atomically: sets status=FAILED, saves error log,
    optionally writes AuditLog, and broadcasts the status change over Supabase Realtime.
    """
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        logger.error("[Worker] Cannot fail document %s — does not exist.", doc_uuid)
        return

    surreal_db.update_document(doc_uuid, {"status": "FAILED", "error_message": error_message})

    if log_audit:
        uploaded_by_id = doc.get("uploaded_by_id")
        user = _resolve_user_by_id(uploaded_by_id)
        log_audit_event(
            AuditEvent(
                action=AuditAction.EXTRACTION_FAILED,
                user=user,
                actor_id=uploaded_by_id,
                document=doc,
                details=details,
            )
        )

    # Broadcast failure outside transaction
    try:
        broadcast_status_change(doc_uuid, "FAILED")
    except Exception as exc:
        logger.debug("[Worker] Failed to broadcast status failure: %s", exc)


def _handle_stage_failure(doc_uuid: str, stage_name: str, exception: Exception) -> None:
    """Centralised failure handler — logs traceback and delegates to _fail_document."""
    err_msg = traceback.format_exc()
    logger.error(
        "[Worker] %s failed for document %s:\n%s",
        stage_name,
        doc_uuid,
        err_msg,
    )
    _fail_document(
        doc_uuid,
        error_message=f"{stage_name} Error:\n{err_msg}",
        details=f"{stage_name} process failed: {exception!s}",
    )


def _prepare_document_for_processing(doc_uuid: str) -> dict | None:
    """
    Lock document row and transition status to EXTRACTING.
    Returns None if document is already finalised, doesn't exist, or budget is exceeded.
    """
    doc = surreal_db.claim_document_for_processing(doc_uuid)
    if not doc:
        logger.info("[Worker] Document %s is not pending or is already claimed. Skipping.", doc_uuid)
        return None

    try:
        check_budget_and_api_limit()
    except Exception as budget_err:
        _fail_document(
            doc_uuid,
            error_message=f"Budget Capped Halt: {budget_err!s}",
            details=f"Pipeline halted before start due to budget breach: {budget_err!s}",
        )
        return None

    uploaded_by_id = doc.get("uploaded_by_id")
    user = _resolve_user_by_id(uploaded_by_id)

    log_audit_event(
        AuditEvent(
            action=AuditAction.EXTRACTION_START,
            user=user,
            actor_id=uploaded_by_id,
            document=doc,
            details=f"Background curation pipeline started for '{doc.get('original_filename')}' (UUID: {doc_uuid}).",
        )
    )

    broadcast_status_change(doc_uuid, "EXTRACTING")
    return doc


def _get_working_path(doc_or_id: Any, download: bool = True) -> tuple[str, str]:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        p = _get_working_path_offline(doc_or_id, download)
        return p, p
    else:
        p = _get_working_path_surreal(doc_or_id, download)
        return p, p


def _resolve_offline_source_doc(doc_or_id: Any):
    from extractor.models import SourceDocument

    if isinstance(doc_or_id, SourceDocument):
        return doc_or_id
    doc_id = doc_or_id
    try:
        import uuid

        try:
            uuid.UUID(str(doc_id))
            return SourceDocument.objects.get(uuid=doc_id)
        except ValueError:
            return SourceDocument.objects.get(id=int(getattr(doc_id, "id", doc_id)))
    except (SourceDocument.DoesNotExist, ValueError):
        raise ValueError(f"Document {doc_id} not found in SQLite")


def _read_offline_file_bytes(doc_file) -> bytes:
    try:
        raw_open = getattr(doc_file, "open", None)
        if callable(raw_open) and not hasattr(raw_open, "_mock_return_value"):
            try:
                with doc_file.open("rb") as f:
                    res = f.read()
                    if isinstance(res, (bytes, bytearray)):
                        return bytes(res)
            except Exception as open_err:
                logger.debug("[Worker] Could not read via doc.file.open(): %s", open_err)
        res = doc_file.read()
        if isinstance(res, (bytes, bytearray)):
            return bytes(res)
        return b""
    except Exception:
        logger.exception("[Worker] Failed to read file from SQLite storage")
        raise


def _get_working_path_offline(doc_or_id: Any, download: bool) -> str:
    doc = _resolve_offline_source_doc(doc_or_id)
    if not download:
        return ""
    if not doc.file or not doc.file.name:
        doc_ref_id = getattr(doc, "id", doc_or_id)
        raise ValueError(f"Document {doc_ref_id} has no file attached")

    content = _read_offline_file_bytes(doc.file)
    import os

    ext = os.path.splitext(doc.file.name)[1]
    fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="offline_worker_")
    with os.fdopen(fd, "wb") as f_out:
        f_out.write(content)
    return temp_path


def _get_working_path_surreal(doc_or_id: Any, download: bool) -> str:
    import os
    import urllib.parse

    from extractor.file_utils import _get_gcs_bucket

    doc: dict[Any, Any] | None
    if isinstance(doc_or_id, dict):
        doc = doc_or_id
        doc_id = doc.get("doc_uuid") or str(doc.get("id", ""))
    elif hasattr(doc_or_id, "doc_uuid"):
        doc_id = str(doc_or_id.doc_uuid)
        doc = surreal_db.get_document(doc_id)
    else:
        doc_id = str(doc_or_id)
        doc = surreal_db.get_document(doc_id)

    if not doc:
        raise ValueError(f"Document {doc_id} not found in SurrealDB")
    if not download:
        return ""
    gcs_uri = doc.get("file")
    if not gcs_uri:
        raise ValueError(f"Document {doc_id} has no GCS file URI")
    if not gcs_uri.startswith("gs://"):
        raise ValueError(f"Document {doc_id} has an invalid GCS URI: {gcs_uri}")
    parsed = urllib.parse.urlparse(gcs_uri)
    bucket_name = parsed.netloc
    blob_name = parsed.path.lstrip("/")
    bucket = _get_gcs_bucket()
    if bucket.name != bucket_name:
        raise ValueError(f"Document {doc_id} is in bucket {bucket_name}, but worker is configured for {bucket.name}")
    blob = bucket.blob(blob_name)
    if not blob.exists():
        raise ValueError(f"Document {doc_id} GCS blob {blob_name} does not exist")
    ext = os.path.splitext(blob_name)[1]
    fd, temp_path = tempfile.mkstemp(suffix=ext, prefix="worker_")
    os.close(fd)
    blob.download_to_filename(temp_path)
    return temp_path


def _get_doc_info_stage1(document_id):
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(document_id))
                doc = SourceDocument.objects.get(uuid=document_id)
            except ValueError:
                doc = SourceDocument.objects.get(id=int(document_id))
        except (SourceDocument.DoesNotExist, ValueError):
            logger.exception("[Worker] Document ID/UUID %s not found in SQLite", document_id)
            raise
        return doc, doc.original_filename.lower(), doc.id
    else:
        doc = surreal_db.get_document(str(document_id))
        if not doc:
            raise ValueError(f"Document {document_id} not found in SurrealDB")
        return doc, doc.get("original_filename", "").lower(), doc.get("doc_uuid")


def _extract_existing_raw(
    doc: Any, doc_id_display: str, working_path: str, lower_name: str
) -> tuple[str, str, int, Decimal, int, int] | None:
    existing_raw = (doc.get("raw_markdown") if isinstance(doc, dict) else getattr(doc, "raw_markdown", None)) or ""
    if len(existing_raw.strip()) > 20:
        logger.info("[Worker/Stage 1] Reusing existing raw_markdown for Document ID: %s", doc_id_display)
        doc_type = (doc.get("document_type") if isinstance(doc, dict) else getattr(doc, "document_type", None)) or (
            "PDF" if lower_name.endswith(".pdf") else "IMAGE"
        )
        page_cnt = (
            doc.get("page_count") if isinstance(doc, dict) else getattr(doc, "page_count", 0)
        ) or _determine_actual_page_count(working_path, doc_type)
        return existing_raw, doc_type, page_cnt, Decimal("0.0"), 0, 0
    return None


def _extract_cached_ocr(
    file_hash: str, working_path: str, lower_name: str
) -> tuple[str, str, int, Decimal, int, int] | None:
    from django.conf import settings

    from extractor import surreal_db

    cached_ocr = (
        surreal_db.kv_cache_get(f"ocr:{file_hash}")
        if (file_hash and not getattr(settings, "SURREALDB_OFFLINE", False))
        else None
    )
    if isinstance(cached_ocr, dict) and cached_ocr.get("raw_markdown"):
        logger.info("[Worker/Stage 1] Found cached OCR result in KV Cache for file_hash %s", file_hash)
        doc_type = cached_ocr.get("document_type") or ("PDF" if lower_name.endswith(".pdf") else "IMAGE")
        page_cnt = cached_ocr.get("page_count") or _determine_actual_page_count(working_path, doc_type)
        return cached_ocr["raw_markdown"], doc_type, page_cnt, Decimal("0.0"), 0, 0
    return None


def _get_existing_or_cached_markdown(
    doc: Any, file_hash: str, doc_id_display: str, working_path: str, lower_name: str
) -> tuple[str, str, int, Decimal, int, int] | None:
    existing_res = _extract_existing_raw(doc, doc_id_display, working_path, lower_name)
    if existing_res:
        return existing_res

    cached_res = _extract_cached_ocr(file_hash, working_path, lower_name)
    if cached_res:
        return cached_res
    return None


def _acquire_stage1_raw_markdown(
    working_path: str, lower_name: str, doc: Any, file_hash: str, doc_id_display: str
) -> tuple[str, str, int, Decimal, int, int]:
    """Helper to route document through cached, local, or multimodal OCR extraction."""
    from django.conf import settings

    from extractor import surreal_db

    cached_result = _get_existing_or_cached_markdown(doc, file_hash, doc_id_display, working_path, lower_name)
    if cached_result:
        return cached_result

    if lower_name.endswith((".txt", ".md", ".markdown")):
        raw_md = process_txt_local(working_path)
        return raw_md, "TXT", _determine_actual_page_count(working_path, "TXT"), Decimal("0.0"), 0, 0

    if lower_name.endswith(".csv"):
        raw_md = process_csv_local(working_path)
        return raw_md, "CSV", _determine_actual_page_count(working_path, "CSV"), Decimal("0.0"), 0, 0

    if lower_name.endswith((".xlsx", ".xls")):
        raw_md = process_excel_local(working_path)
        return raw_md, "EXCEL", _determine_actual_page_count(working_path, "EXCEL"), Decimal("0.0"), 0, 0

    if lower_name.endswith(".json"):
        raw_md = process_json_local(working_path)
        return raw_md, "JSON", _determine_actual_page_count(working_path, "JSON"), Decimal("0.0"), 0, 0

    if lower_name.endswith(".pdf"):
        # 1. Try zero-cost native digital text extraction first
        local_pdf_text, pdf_pages = process_pdf_local(working_path)
        if local_pdf_text:
            logger.info(
                "[Worker/Stage 1] Extracted native digital text from PDF at $0.00 cost (Doc: %s, Pages: %d)",
                doc_id_display,
                pdf_pages,
            )
            return local_pdf_text, "PDF", pdf_pages, Decimal("0.0"), 0, 0

    # Multimodal OCR fallback for scanned PDFs or images
    doc_type = "PDF" if lower_name.endswith(".pdf") else "IMAGE"
    from extractor.llm_gateway import MODEL_GEMINI_FLASH_LITE

    ocr_res = run_stage1_multimodal_ocr(
        working_path, model_name=getattr(settings, "GEMINI_MODEL_BATCH", MODEL_GEMINI_FLASH_LITE)
    )
    raw_md = ocr_res["raw_markdown"]
    page_cnt = _determine_actual_page_count(working_path, doc_type)

    if file_hash and raw_md and not getattr(settings, "SURREALDB_OFFLINE", False):
        surreal_db.kv_cache_set(
            f"ocr:{file_hash}",
            {"raw_markdown": raw_md, "document_type": doc_type, "page_count": page_cnt},
        )

    return (
        raw_md,
        doc_type,
        page_cnt,
        Decimal(str(ocr_res["cost_usd"])),
        ocr_res["input_tokens"],
        ocr_res["output_tokens"],
    )


def _run_stage1(working_path: str, document_id: str | int) -> Any:
    """Stage 1: OCR / local parsing."""
    doc, lower_name, doc_id_display = _get_doc_info_stage1(document_id)
    file_hash = (doc.get("file_hash") if isinstance(doc, dict) else getattr(doc, "file_hash", None)) or ""

    (
        raw_markdown,
        doc_type_detected,
        page_count_detected,
        stage1_cost,
        stage1_input_tokens,
        stage1_output_tokens,
    ) = _acquire_stage1_raw_markdown(working_path, lower_name, doc, file_hash, doc_id_display)

    if getattr(settings, "SURREALDB_OFFLINE", False):
        with transaction.atomic():
            from extractor.models import SourceDocument

            try:
                import uuid

                try:
                    uuid.UUID(str(document_id))
                    doc_ref = SourceDocument.objects.select_for_update().get(uuid=document_id)
                except ValueError:
                    doc_ref = SourceDocument.objects.select_for_update().get(id=int(document_id))
            except (SourceDocument.DoesNotExist, ValueError):
                doc_ref = doc
            doc_ref.document_type = doc_type_detected
            doc_ref.page_count = page_count_detected
            doc_ref.raw_markdown = raw_markdown
            doc_ref.input_tokens += stage1_input_tokens
            doc_ref.output_tokens += stage1_output_tokens
            doc_ref.cost_usd += stage1_cost
            doc_ref.status = "REFINING"
            doc_ref.save()
        broadcast_status_change(str(doc_ref.uuid), "REFINING")
        return doc_ref
    else:
        current_input = doc.get("input_tokens") or 0
        current_output = doc.get("output_tokens") or 0
        current_cost = doc.get("cost_usd") or 0.0

        updated_data = {
            "document_type": doc_type_detected,
            "page_count": page_count_detected,
            "raw_markdown": raw_markdown,
            "input_tokens": current_input + stage1_input_tokens,
            "output_tokens": current_output + stage1_output_tokens,
            "cost_usd": float(Decimal(str(current_cost)) + stage1_cost),
            "status": "REFINING",
        }
        surreal_db.update_document(str(doc.get("doc_uuid")), updated_data)
        broadcast_status_change(str(doc.get("doc_uuid")), "REFINING")
        return doc


def _sanitise_yaml_block(raw: str) -> str:
    """
    Pre-process LLM-generated YAML before parsing.

    Common failure modes from Arabic / Islamic text authors:
      author: 'Abdul-'Azīz ibn 'Abdullah ibn Bāz
      title:  The Manner of Performing: A Guide

    Strategy — for each scalar line (key: value) that is NOT already quoted
    with double-quotes, wrap the value in double-quotes and escape any
    embedded double-quotes.  This is safe because we only touch lines that
    look like plain YAML scalars and always preserve the key.
    """
    import re

    fixed_lines = []
    # Matches: optional leading whitespace, key, colon+space, then the value
    line_re = re.compile(r"^(\s*\w+\s*:\s)(.+)$")
    for line in raw.splitlines():
        m = line_re.match(line)
        if m:
            prefix, value = m.group(1), m.group(2).strip()
            # Leave values already properly double-quoted as-is
            if value.startswith('"') and value.endswith('"'):
                fixed_lines.append(line)
                continue
            # Strip single-quote wrapping that the LLM sometimes adds
            if value.startswith("'") and value.endswith("'") and len(value) > 1:
                value = value[1:-1]
            # Escape any existing double-quotes inside the value
            value = value.replace('"', '\\"')
            fixed_lines.append(f'{prefix}"{value}"')
        else:
            fixed_lines.append(line)
    return "\n".join(fixed_lines)


def _clean_val(val):
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in ("unknown", "n/a", "none", "null", "undefined", ""):
        return None
    return s


def _extract_meta_dict(meta, default_title, default_author, default_lang, default_doc_type) -> dict:
    return {
        "title": _clean_val(meta.get("title")) or default_title,
        "author": _clean_val(meta.get("author")) or default_author,
        "language": _clean_val(meta.get("language")) or default_lang,
        "document_type": _clean_val(meta.get("document_type")) or default_doc_type,
        "semantic_signature": _clean_val(meta.get("semantic_signature")),
        "isbn": _clean_val(meta.get("isbn")),
        "source_link": _clean_val(meta.get("source_link")),
        "translator": _clean_val(meta.get("translator")),
        "publisher": _clean_val(meta.get("publisher")),
        "publication_year": _clean_val(meta.get("publication_year")),
        "license_type": _clean_val(meta.get("license_type")),
        "doi": _clean_val(meta.get("doi")),
    }


def _parse_yaml_metadata(
    yaml_metadata_block: str,
    default_title: str | None,
    default_author: str | None,
    default_lang: str | None,
    default_doc_type: str | None,
) -> dict:
    """Parse YAML metadata block, fallback to defaults on error."""
    import yaml  # type: ignore[import-untyped]

    parsed = {
        "title": default_title,
        "author": default_author,
        "language": default_lang,
        "document_type": default_doc_type,
        "semantic_signature": None,
        "isbn": None,
        "source_link": None,
        "translator": None,
        "publisher": None,
        "publication_year": None,
        "license_type": None,
        "doi": None,
    }

    if not yaml_metadata_block:
        return parsed

    for attempt, block in enumerate([yaml_metadata_block, _sanitise_yaml_block(yaml_metadata_block)]):
        try:
            meta_raw = yaml.safe_load(block)
            if not isinstance(meta_raw, dict):
                raise ValueError("YAML metadata block is not a dictionary")
            meta = {str(k).strip().lower(): v for k, v in meta_raw.items()}
            parsed = _extract_meta_dict(meta, default_title, default_author, default_lang, default_doc_type)
            break
        except Exception as yaml_err:
            if attempt == 0:
                logger.debug("[Worker] YAML parse failed on raw block, retrying with sanitiser: %s", yaml_err)
            else:
                logger.warning("[Worker] Metadata YAML parsing failed (using defaults): %s", yaml_err)

    return parsed


# Maximum characters to send to Stage 2 in a single LLM call.
# Gemini 2.5 Flash has a large context window; keep the conservative character cap below.
# We use a conservative 600K chars per chunk to stay well within limits and
# leave room for the system prompt + output.
_STAGE2_CHUNK_CHARS = 600_000


def _split_markdown_into_chunks(text: str, max_chars: int = _STAGE2_CHUNK_CHARS) -> list[str]:
    """
    Splits large markdown text into chunks of at most *max_chars* characters,
    trying to break on heading boundaries (## or #) or blank lines rather than
    mid-sentence. This preserves document structure across chunks.
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    remaining = text
    while len(remaining) > max_chars:
        # Try to break on a heading line within the last 20% of the chunk window
        window = remaining[:max_chars]
        search_start = int(max_chars * 0.80)

        # Search backwards for a heading boundary
        best_break = -1
        for marker in ("\n## ", "\n# ", "\n### ", "\n\n"):
            idx = window.rfind(marker, search_start)
            if idx > best_break:
                best_break = idx

        # Fall back to last blank line or just the char limit
        if best_break <= 0:
            best_break = window.rfind("\n\n", search_start)
        if best_break <= 0:
            best_break = max_chars

        chunks.append(remaining[:best_break].strip())
        remaining = remaining[best_break:].strip()

    if remaining:
        chunks.append(remaining)

    return chunks


def _is_unknown_value(val) -> bool:
    """Return True if the value is blank or an 'unknown' placeholder."""
    return not val or str(val).strip().lower() in ("unknown", "n/a", "none", "null", "undefined", "")


def _set_val(obj, key, val):
    if isinstance(obj, dict):
        obj[key] = val
    else:
        setattr(obj, key, val)


def _get_val(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    else:
        return getattr(obj, key, default)


def _resolve_doc_title(parsed_title: Any, orig_filename: str) -> str:
    import os

    t_val = _truncate(parsed_title, _MAX_TITLE_LEN)
    if _is_unknown_value(t_val):
        t_val = os.path.splitext(orig_filename)[0].replace("_", " ").replace("-", " ").strip()
    return t_val or orig_filename or "Untitled"


def _resolve_doc_author(parsed_author: Any, orig_filename: str) -> str:
    a_val = _truncate(parsed_author, _MAX_AUTHOR_LEN)
    if _is_unknown_value(a_val):
        a_val = "Anonymous"
    if "sahih" in orig_filename.lower() and (
        a_val == "Anonymous" or "divinely" in a_val.lower() or "anonymous" in a_val.lower()
    ):
        a_val = "Sahih International"
    return a_val


def _update_doc_metadata(doc_ref, parsed_meta: dict):
    """Apply parsed YAML metadata values to a SourceDocument instance or dictionary (inside atomic block)."""
    orig_filename = _get_val(doc_ref, "original_filename", "")

    parsed_doc_type = parsed_meta.get("document_type")
    parsed_sig = parsed_meta.get("semantic_signature")
    parsed_pub = parsed_meta.get("publisher")
    parsed_year = parsed_meta.get("publication_year")
    parsed_lic = parsed_meta.get("license_type")
    parsed_doi = parsed_meta.get("doi")

    _set_val(doc_ref, "title", _resolve_doc_title(parsed_meta.get("title"), orig_filename))
    _set_val(doc_ref, "author", _resolve_doc_author(parsed_meta.get("author"), orig_filename))

    l_val = _truncate(parsed_meta.get("language"), _MAX_LANGUAGE_LEN)
    if _is_unknown_value(l_val):
        l_val = "English"
    _set_val(doc_ref, "language", l_val)

    if parsed_doc_type:
        _set_val(doc_ref, "document_type", _truncate(parsed_doc_type, _MAX_DOCTYPE_LEN))
    if parsed_sig:
        _set_val(doc_ref, "semantic_signature", _truncate(parsed_sig, _MAX_SIG_LEN))

    if parsed_pub and not _is_unknown_value(parsed_pub):
        _set_val(doc_ref, "publisher", _truncate(parsed_pub, 255))

    if parsed_year and not _is_unknown_value(parsed_year):
        import re

        year_match = re.search(r"\d{4}", str(parsed_year))
        if year_match:
            _set_val(doc_ref, "publication_year", year_match.group(0))

    if parsed_lic and not _is_unknown_value(parsed_lic):
        _set_val(doc_ref, "license_type", _truncate(parsed_lic, 100))

    if parsed_doi and not _is_unknown_value(parsed_doi):
        clean_doi = (
            str(parsed_doi).replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        )  # NOSONAR python:S5332 -- Strip URL scheme from DOI identifier
        _set_val(doc_ref, "doi", _truncate(clean_doi, 255))


def _execute_stage2_chunks_refinement(
    raw_markdown: str, selected_model: str, file_hash: str
) -> tuple[str, str, list, float, int, int]:
    """Helper to process markdown chunks through LLM editorial refinement."""

    chunks = _split_markdown_into_chunks(raw_markdown)
    total_chunks = len(chunks)

    refined_parts: list[str] = []
    yaml_metadata_block = ""
    qa_dataset: list = []
    stage2_cost = 0.0
    stage2_input_tokens = 0
    stage2_output_tokens = 0

    for idx, chunk in enumerate(chunks, 1):
        try:
            chunk_results = run_stage2_editorial_refinement(chunk, model_name=selected_model)
        except Exception as chunk_err:
            logger.exception("[Worker] Stage 2 chunk %d/%d failed: %s", idx, total_chunks, chunk_err)
            refined_parts.append(chunk)
            continue

        refined_parts.append(chunk_results["refined_markdown"])
        if idx == 1:
            yaml_metadata_block = chunk_results["yaml_metadata"]

        for qa in chunk_results.get("qa_dataset", []):
            if len(qa_dataset) < 20:
                qa_dataset.append(qa)

        stage2_cost += float(chunk_results["cost_usd"])
        stage2_input_tokens += chunk_results["input_tokens"]
        stage2_output_tokens += chunk_results["output_tokens"]

    refined_markdown = "\n\n---\n\n".join(p for p in refined_parts if p.strip())

    if file_hash and refined_markdown and not getattr(settings, "SURREALDB_OFFLINE", False):
        surreal_db.kv_cache_set(
            f"refine:{file_hash}",
            {
                "refined_markdown": refined_markdown,
                "yaml_metadata": yaml_metadata_block,
                "qa_dataset": qa_dataset,
            },
        )

    return (
        refined_markdown,
        yaml_metadata_block,
        qa_dataset,
        stage2_cost,
        stage2_input_tokens,
        stage2_output_tokens,
    )


def _run_stage2(raw_markdown: str, doc_uuid: str) -> dict:
    """Stage 2: Editorial reasoning refinement."""
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        raise ValueError(f"Document {doc_uuid} not found")
    logger.info("[Worker] Launching Stage 2 Refinement for Document UUID: %s", doc_uuid)

    existing_refined = (
        doc.get("refined_markdown") if isinstance(doc, dict) else getattr(doc, "refined_markdown", None)
    ) or ""
    existing_yaml = (doc.get("yaml_metadata") if isinstance(doc, dict) else getattr(doc, "yaml_metadata", None)) or ""
    file_hash = (doc.get("file_hash") if isinstance(doc, dict) else getattr(doc, "file_hash", None)) or ""
    cached_refine = (
        surreal_db.kv_cache_get(f"refine:{file_hash}")
        if (file_hash and not getattr(settings, "SURREALDB_OFFLINE", False))
        else None
    )

    if len(existing_refined.strip()) > 20 and len(existing_yaml.strip()) > 5:
        logger.info("[Worker/Stage 2] Reusing existing refined_markdown for Document UUID: %s", doc_uuid)
        return doc

    try:
        settings_obj = surreal_db.get_system_settings()
        selected_model = settings_obj.get("selected_model", "auto")
    except Exception as settings_err:
        logger.warning("[Stage2] Could not fetch system settings: %s", settings_err)
        selected_model = "auto"

    if isinstance(cached_refine, dict) and cached_refine.get("refined_markdown"):
        logger.info("[Worker/Stage 2] Found cached Refinement result for file_hash %s", file_hash)
        refined_markdown = cached_refine["refined_markdown"]
        yaml_metadata_block = cached_refine.get("yaml_metadata", "")
        qa_dataset = cached_refine.get("qa_dataset", [])
        stage2_cost, stage2_input_tokens, stage2_output_tokens = 0.0, 0, 0
    else:
        (
            refined_markdown,
            yaml_metadata_block,
            qa_dataset,
            stage2_cost,
            stage2_input_tokens,
            stage2_output_tokens,
        ) = _execute_stage2_chunks_refinement(raw_markdown, selected_model, file_hash)

    parsed_meta = _parse_yaml_metadata(
        yaml_metadata_block,
        doc.get("title", ""),
        doc.get("author", ""),
        doc.get("language", ""),
        doc.get("document_type", ""),
    )

    doc_ref = surreal_db.get_document(doc_uuid)
    _update_doc_metadata(doc_ref, parsed_meta)

    input_tokens = _get_val(doc_ref, "input_tokens", 0) + stage2_input_tokens
    output_tokens = _get_val(doc_ref, "output_tokens", 0) + stage2_output_tokens
    cost_usd = float(_get_val(doc_ref, "cost_usd", 0.0)) + stage2_cost

    updated = surreal_db.update_document(
        doc_uuid,
        {
            "refined_markdown": refined_markdown,
            "yaml_metadata": yaml_metadata_block,
            "qa_dataset": qa_dataset,
            "title": _get_val(doc_ref, "title"),
            "author": _get_val(doc_ref, "author"),
            "language": _get_val(doc_ref, "language"),
            "document_type": _get_val(doc_ref, "document_type"),
            "semantic_signature": _get_val(doc_ref, "semantic_signature"),
            "publisher": _get_val(doc_ref, "publisher"),
            "publication_year": _get_val(doc_ref, "publication_year"),
            "license_type": _get_val(doc_ref, "license_type"),
            "doi": _get_val(doc_ref, "doi"),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_usd": cost_usd,
            "status": "EMBEDDING",
        },
    )

    broadcast_status_change(doc_uuid, "EMBEDDING")
    return updated


def _build_chunk_payload(
    chunk_index: int, chunk_text: str, emb: list[float], doc_language: str
) -> tuple[dict, int, str]:
    """Helper to parse chunk markers and construct SurrealDB chunk payload."""
    page_match = re.search(r"## Page (\d+)", chunk_text, re.IGNORECASE)
    current_page = int(page_match.group(1)) if page_match else 1
    chap_match = re.search(r"(?:###?|Chapter|Surah|Hadith)\s+([^\n]+)", chunk_text, re.IGNORECASE)
    current_chapter = chap_match.group(1).strip() if chap_match else ""
    anchor_slug = f"page-{current_page}" if not current_chapter else f"p{current_page}-{slugify(current_chapter[:30])}"

    payload = {
        "chunk_index": chunk_index,
        "content": chunk_text,
        "token_count": len(chunk_text.split()),
        "language": doc_language,
        "page_number": current_page,
        "chapter_title": current_chapter,
        "anchor_id": anchor_slug,
        "embedding": emb,
    }
    return payload, current_page, current_chapter


def _run_stage3(text_for_chunks: str, doc_uuid: str) -> dict:
    """
    Stage 3: Semantic chunking and SurrealDB HNSW vector embedding.
    """
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        raise ValueError(f"Document {doc_uuid} not found")
    logger.info("[Worker] Segmenting markdown for Document UUID: %s", doc_uuid)

    lang = (doc.get("language") or "").lower()
    chunk_size = 500 if "arabic" in lang or "ar" in lang else 1200
    chunks = chunk_document_semantically(text_for_chunks, max_chunk_size=chunk_size)

    retention_days = int(getattr(settings, "DATA_RETENTION_DAYS", 30))

    if chunks:
        embeddings = generate_surreal_embeddings(chunks, model_name="text-embedding-004")
        doc_lang = doc.get("language") or ""
        chunk_payloads = [
            _build_chunk_payload(i, chunk_text, emb, doc_lang)[0]
            for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings))
        ]

        # Delete old SurrealDB chunks first, then insert new ones atomically
        surreal_db.recreate_chunks(doc_uuid, chunk_payloads)

        expires_at = timezone.now() + timedelta(days=retention_days)
        existing_page_count = _get_val(doc, "page_count") or 0
        final_page_count = existing_page_count if existing_page_count > 0 else len(chunks)
        updated = surreal_db.update_document(
            doc_uuid,
            {
                "page_count": final_page_count,
                "status": "COMPLETED",
                "expires_at": format_datetime(expires_at),
            },
        )
    else:
        surreal_db.delete_chunks(doc_uuid)

        expires_at = timezone.now() + timedelta(days=retention_days)
        existing_page_count = _get_val(doc, "page_count") or 0
        final_page_count = max(0, existing_page_count)
        updated = surreal_db.update_document(
            doc_uuid,
            {
                "page_count": final_page_count,
                "status": "COMPLETED",
                "expires_at": format_datetime(expires_at),
            },
        )

    broadcast_status_change(doc_uuid, "COMPLETED")
    return updated


def _check_pipeline_active(doc_uuid: str) -> bool:
    """
    Check if a document pipeline is still active and valid to continue.
    Returns False if document was deleted or marked FAILED/canceled by user.
    """
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        logger.info("[Worker] Document %s was deleted. Aborting pipeline.", doc_uuid)
        return False
    if doc.get("status") == "FAILED":
        logger.info("[Worker] Document %s was cancelled or failed. Aborting pipeline.", doc_uuid)
        return False
    return True


def _run_pipeline_stages(initial_doc: dict, working_path: str, doc_uuid: str) -> bool:
    from extractor.surreal_db import _model_to_dict

    if not isinstance(initial_doc, dict):
        initial_doc = _model_to_dict(initial_doc)

    logger.info("[Worker] Running pipeline stages for document: %s", initial_doc.get("original_filename"))

    # Pre-Stage 1 check
    if not _check_pipeline_active(doc_uuid):
        return False

    # Stage 1
    try:
        doc = _run_stage1(working_path, doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)
    except Exception as exc:
        _handle_stage_failure(doc_uuid, "Stage 1", exc)
        return False

    # Check cancellation before budget & Stage 2
    if not _check_pipeline_active(doc_uuid):
        return False

    # Mid-pipeline budget circuit breaker
    try:
        check_budget_and_api_limit()
    except Exception as budget_err:
        logger.warning("[Worker] Mid-pipeline budget limit breached: %s", budget_err)
        _fail_document(
            doc_uuid,
            error_message=f"Mid-Pipeline Budget Capped Halt: {budget_err!s}",
            details=f"Mid-pipeline budget breach: {budget_err!s}",
        )
        return False

    # Check cancellation before Stage 2
    if not _check_pipeline_active(doc_uuid):
        return False

    # Stage 2
    try:
        doc = _run_stage2(doc.get("raw_markdown", ""), doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)
    except Exception as exc:
        _handle_stage_failure(doc_uuid, "Stage 2", exc)
        return False

    # Check cancellation before Stage 3
    if not _check_pipeline_active(doc_uuid):
        return False

    # Stage 3
    try:
        text_for_chunks = doc.get("refined_markdown") or doc.get("raw_markdown", "")
        doc = _run_stage3(text_for_chunks, doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)

        logger.info("[Worker] Pipeline completed successfully for UUID %s!", doc_uuid)

        uploaded_by_id = doc.get("uploaded_by_id")
        user = _resolve_user_by_id(uploaded_by_id)

        log_audit_event(
            AuditEvent(
                action=AuditAction.EXTRACTION_COMPLETED,
                user=user,
                actor_id=uploaded_by_id,
                document=doc,
                details=(
                    f"Curation pipeline completed. Pages: {doc.get('page_count')}. "
                    f"Cost: ${doc.get('cost_usd'):.6f} USD. "
                    f"Tokens in: {doc.get('input_tokens')}, out: {doc.get('output_tokens')}."
                ),
            ),
        )
        return True
    except Exception as exc:
        _handle_stage_failure(doc_uuid, "Stage 3", exc)
        return False


def process_document_task(payload: dict) -> None:
    """
    Main pipeline handler: OCR → Refinement → Embedding.
    Receives a Cloud Tasks payload dict with document_uuid or document_id.
    """
    doc_uuid = payload.get("document_uuid") or payload.get("document_id")
    if not doc_uuid:
        logger.error("[Worker] process_document_task called with missing document_uuid")
        return

    logger.info("[Worker] Starting processing pipeline for Document UUID: %s", doc_uuid)
    try:
        doc = _prepare_document_for_processing(doc_uuid)
        if not doc:
            return

        temp_local_path = None
        try:
            doc_id_str = str(getattr(doc, "id", doc))
            working_path = _get_working_path(doc_id_str)
            temp_local_path = working_path[0] if isinstance(working_path, tuple) else working_path
            _run_pipeline_stages(doc, temp_local_path, doc_uuid)
        finally:
            if temp_local_path and os.path.exists(temp_local_path):
                try:
                    os.unlink(temp_local_path)
                except Exception as clean_err:
                    logger.warning("[Worker] Error removing temp file %s: %s", temp_local_path, clean_err)

    except Exception as exc:
        err_msg = traceback.format_exc()
        logger.exception("[Worker] Uncaught outer exception: %s", err_msg)
        try:
            _fail_document(
                doc_uuid,
                error_message=f"Pipeline Crashed:\n{err_msg}",
                details=f"Uncaught outer pipeline crash: {exc!s}",
            )
        except Exception as db_err:
            logger.exception("[Worker] Double-fault! Could not save crash to DB: %s", db_err)


def reembed_edited_document_task(payload: dict) -> None:
    """
    Lightweight task to re-chunk and re-embed a document after manual user edits.
    Explicitly purges old SurrealDB chunks before inserting new ones.
    """
    doc_uuid = payload.get("document_uuid") or payload.get("document_id")
    if not doc_uuid:
        logger.error("[Worker] reembed_edited_document_task called with missing document_uuid")
        return

    logger.info("[Worker] Re-embedding Document UUID: %s", doc_uuid)

    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        logger.error("[Worker] Document %s does not exist.", doc_uuid)
        return

    surreal_db.update_document(doc_uuid, {"status": "EMBEDDING"})
    broadcast_status_change(doc_uuid, "EMBEDDING")

    try:
        text_for_chunks = doc.get("refined_markdown") or doc.get("raw_markdown") or ""
        lang = (doc.get("language") or "").lower()
        chunk_size = 500 if "arabic" in lang or "ar" in lang else 1200
        chunks = chunk_document_semantically(text_for_chunks, max_chunk_size=chunk_size)

        # Always purge old SurrealDB chunks on re-embed
        surreal_db.delete_chunks(doc_uuid)

        if chunks:
            check_budget_and_api_limit()
            embeddings = generate_surreal_embeddings(chunks, model_name="text-embedding-004")
            chunk_payloads = [
                {
                    "chunk_index": i,
                    "content": ct,
                    "token_count": len(ct.split()),
                    "language": doc.get("language") or "",
                    "embedding": emb,
                }
                for i, (ct, emb) in enumerate(zip(chunks, embeddings))
            ]
            surreal_db.recreate_chunks(doc_uuid, chunk_payloads)

            existing_page_count = _get_val(doc, "page_count") or 0
            final_page_count = existing_page_count if existing_page_count > 0 else len(chunks)
            surreal_db.update_document(doc_uuid, {"page_count": final_page_count, "status": "COMPLETED"})
        else:
            existing_page_count = _get_val(doc, "page_count") or 0
            final_page_count = max(0, existing_page_count)
            surreal_db.update_document(doc_uuid, {"page_count": final_page_count, "status": "COMPLETED"})

        logger.info("[Worker] Re-embedding successful for Document UUID: %s!", doc_uuid)
        broadcast_status_change(doc_uuid, "COMPLETED")

    except Exception as exc:
        err_msg = traceback.format_exc()
        logger.exception("[Worker] Exception in re-embedding: %s", err_msg)
        _fail_document(
            doc_uuid,
            error_message=f"Re-embedding Failure:\n{err_msg}",
            details=f"Re-embedding failed: {exc!s}",
            log_audit=False,
        )


def _resolve_local_doc_obj(doc_uuid):
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                return SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                return SourceDocument.objects.get(id=int(doc_uuid or 0))
        except (ValueError, SourceDocument.DoesNotExist) as lookup_err:
            logger.debug("[Cron] Could not resolve doc_uuid %s: %s", doc_uuid, lookup_err)
    return None


def _delete_physical_file(doc_obj, file_rel_path, file_hash):
    try:
        if doc_obj:
            doc_obj.file.delete(save=False)
        else:
            if default_storage.exists(file_rel_path):
                default_storage.delete(file_rel_path)
    except Exception as exc:
        logger.warning("[Cron] Failed to delete physical file for hash %s: %s", file_hash, exc)


def _cleanup_single_expired_doc(doc: dict, hash_counts: dict, surreal_db):
    """Purge one expired document: physical file, SurrealDB chunks, storage JSON, audit log."""
    file_hash = doc.get("file_hash")
    doc_uuid = doc.get("doc_uuid")
    file_rel_path = doc.get("file", "")

    total_refs = hash_counts.get(file_hash, 0)
    shared_references = max(0, total_refs - 1)

    doc_obj = _resolve_local_doc_obj(doc_uuid)

    if shared_references == 0:
        logger.info("[Cron] Purging file hash %s from storage.", file_hash)
        _delete_physical_file(doc_obj, file_rel_path, file_hash)
    else:
        logger.info(
            "[Cron] Skipping physical delete for hash %s (referenced by %s records).", file_hash, shared_references
        )

    if file_hash in hash_counts:
        hash_counts[file_hash] = max(0, hash_counts[file_hash] - 1)

    # Cascade delete SurrealDB chunks for compliance
    try:
        surreal_db.delete_chunks(doc_uuid)
    except Exception as exc:
        logger.warning("[Cron] Failed to delete SurrealDB chunks for %s: %s", doc_uuid, exc)

    # Write audit log before deletion
    log_audit_event(
        AuditEvent(
            action=AuditAction.DELETE,
            user=None,
            document=doc,
            details=f"GDPR retention cleanup: document '{doc.get('original_filename')}' (UUID: {doc_uuid}) expired and purged.",
        )
    )

    surreal_db.delete_document(doc_uuid)


def _fetch_expired_docs_offline(now, surreal_db):
    from django.db.models import Count

    from extractor.models import SourceDocument

    expired_qs = SourceDocument.objects.filter(expires_at__lte=now)
    expired_docs = [surreal_db._model_to_dict(d) for d in expired_qs]
    if not expired_docs:
        return [], {}

    expired_hashes = {doc.get("file_hash") for doc in expired_docs if doc.get("file_hash")}
    hash_counts = dict.fromkeys(expired_hashes, 0)
    if expired_hashes:
        counts = (
            SourceDocument.objects.filter(file_hash__in=expired_hashes).values("file_hash").annotate(count=Count("id"))
        )
        for item in counts:
            hash_counts[item["file_hash"]] = item["count"]
    return expired_docs, hash_counts


def _fetch_expired_docs_surreal(now_str, surreal_db):
    expired_sql = "SELECT * FROM documents WHERE expires_at <= <datetime> $now;"
    expired_docs = surreal_db._first_result(surreal_db._run(expired_sql, {"now": now_str}))

    if not expired_docs:
        return [], {}

    expired_hashes = {doc.get("file_hash") for doc in expired_docs if doc.get("file_hash")}
    hash_counts = dict.fromkeys(expired_hashes, 0)
    if expired_hashes:
        count_sql = (
            "SELECT file_hash, count() AS n FROM documents WHERE file_hash IN $expired_hashes GROUP BY file_hash;"
        )
        res = surreal_db._first_result(surreal_db._run(count_sql, {"expired_hashes": list(expired_hashes)}))
        if res:
            for row in res:
                hash_counts[row.get("file_hash")] = row.get("n", 0)
    return expired_docs, hash_counts


def cleanup_expired_documents_task(_payload: dict | None = None) -> None:
    """
    Reference-counted document garbage disposal.
    Cascades SurrealDB chunk deletions on expiry.
    Purges expired SurrealDB RAG cache entries.
    Writes audit entries for each purged document.
    """
    from django.conf import settings

    from extractor import surreal_db

    logger.info("[Cron] Starting reference-counted expired document cleanup...")
    now = timezone.now()
    now_str = format_datetime(now)

    if getattr(settings, "SURREALDB_OFFLINE", False):
        expired_docs, hash_counts = _fetch_expired_docs_offline(now, surreal_db)
    else:
        expired_docs, hash_counts = _fetch_expired_docs_surreal(now_str, surreal_db)

    purged_count = 0
    for doc in expired_docs:
        _cleanup_single_expired_doc(doc, hash_counts, surreal_db)
        purged_count += 1

    # Purge expired SurrealDB RAG cache entries
    try:
        pruned = surreal_db.purge_expired_rag_cache()
        if pruned:
            logger.info("[Cron] Pruned %s expired SurrealDB RAG cache entries.", pruned)
    except Exception as exc:
        logger.warning("[Cron] Failed to purge expired RAG cache: %s", exc)

    logger.info("[Cron] Cleanup finished. Deleted %s expired records.", purged_count)


def _reap_single_stale_doc(doc: dict) -> bool:
    """Check one stale document; auto-retry if under retry limit or mark FAILED. Returns True if reaped."""
    doc_uuid = str(doc.get("doc_uuid") or "")
    if not doc_uuid:
        return False

    retry_count = doc.get("retry_count", 0)
    current_status = doc.get("status", "UNKNOWN")

    # If document is stuck in PENDING or had a worker restart and has retries left, auto-re-enqueue it
    if retry_count < 3:
        logger.info(
            "[Reaper] Document %s was stuck in '%s' (attempt %s/3). Auto-re-enqueuing task...",
            doc_uuid,
            current_status,
            retry_count + 1,
        )
        surreal_db.update_document(
            doc_uuid,
            {
                "status": "PENDING",
                "retry_count": retry_count + 1,
                "error_message": "",
            },
        )
        try:
            from django.conf import settings

            from extractor import cloud_tasks

            if getattr(settings, "SURREALDB_OFFLINE", False):
                doc_id = doc.get("id")
                cloud_tasks.enqueue("process_document", {"document_id": doc_id})
            else:
                cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})
            logger.info("[Reaper] Successfully auto-re-enqueued document %s", doc_uuid)
            return True
        except Exception as enq_err:
            logger.warning("[Reaper] Auto-retry enqueue failed for %s: %s", doc_uuid, enq_err)

    surreal_db.update_document(
        doc_uuid,
        {
            "status": "FAILED",
            "error_message": (
                "Task timed out or terminated unexpectedly. "
                "The background worker may have scaled down, been preempted, or queue permissions were missing."
            ),
        },
    )
    logger.warning("[Reaper] Reaped stale document task %s (was %s, max retries reached).", doc_uuid, current_status)

    uploaded_by_id = doc.get("uploaded_by_id")
    user = _resolve_user_by_id(uploaded_by_id)

    # Write audit log for reaped task
    log_audit_event(
        AuditEvent(
            action=AuditAction.EXTRACTION_FAILED,
            user=user,
            actor_id=uploaded_by_id,
            document=doc,
            details=(
                f"[Reaper] Document '{doc.get('original_filename')}' was stuck in '{current_status}' for >5 minutes "
                "and has reached max retries, marked as FAILED."
            ),
        ),
    )

    # Broadcast status update so dashboard updates without hard reload
    try:
        broadcast_status_change(doc_uuid, "FAILED")
    except Exception as exc:
        logger.debug("[Reaper] Failed to broadcast status failure: %s", exc)

    return True


def reap_stale_tasks(_payload: dict | None = None) -> int:
    """
    Scans for documents stuck in PENDING, EXTRACTING, REFINING, or EMBEDDING for >5 minutes.
    Automatically re-enqueues them up to 3 times, or marks them FAILED if retries are exhausted.
    """
    logger.info("[Reaper] Scanning for stale or stuck tasks...")
    stale_threshold = timezone.now() - timezone.timedelta(minutes=5)
    stale_threshold_str = format_datetime(stale_threshold)

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        stale_qs = SourceDocument.objects.filter(
            status__in=["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"], updated_at__lte=stale_threshold
        )
        stale_docs = [surreal_db._model_to_dict(d) for d in stale_qs]
    else:
        stale_sql = "SELECT * FROM documents WHERE status INSIDE $states AND updated_at <= <datetime> $threshold;"
        stale_docs = surreal_db._first_result(
            surreal_db._run(
                stale_sql,
                {"states": ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"], "threshold": stale_threshold_str},
            )
        )
    reaped_count = 0

    for doc in stale_docs:
        if _reap_single_stale_doc(doc):
            reaped_count += 1

    if reaped_count > 0:
        logger.info("[Reaper] Successfully recovered or reaped %s stuck tasks.", reaped_count)

    return reaped_count


def store_user_memory_task(payload: dict) -> None:
    """
    Cloud Tasks receiver: Distill a user preference statement, generate its embedding,
    and index it in SurrealDB.
    """
    user_id = payload.get("user_id")
    raw_text = payload.get("text", "").strip()
    if not user_id or not raw_text:
        return

    from django.contrib.auth import get_user_model

    from extractor import surreal_db
    from extractor.llm_gateway import (
        _init_refinement_client,
        _resolve_model_name,
        execute_generate_content_with_fallback,
    )
    from extractor.rag import generate_surreal_embeddings

    user_model = get_user_model()
    try:
        user = user_model.objects.get(id=user_id)
    except user_model.DoesNotExist:
        logger.warning("[Memory Task] User with ID %s does not exist. Aborting.", user_id)
        return

    distill_prompt = (
        "You are an AI memory compiler for a RAG search assistant.\n"
        "Your task is to analyze the user's query and extract their formatting, style, or language preference.\n"
        "Convert the raw preference query into a clean, concise, third-person declarative preference statement (e.g. 'User prefers concise summaries' or 'User wants classical references').\n"
        "If the text does NOT describe a clear style/formatting preference, output only 'NONE'.\n"
        f'User Query: "{raw_text}"\n'
        "Distilled Preference (or 'NONE'):"
    )

    client = _init_refinement_client()
    model = _resolve_model_name("google/gemini-3.1-flash-lite")

    try:
        response, _ = execute_generate_content_with_fallback(client, model, contents=[distill_prompt])
        distilled = response.text.strip().strip("\"'").rstrip(".")
    except Exception as exc:
        logger.warning("[Memory Task] Gemini preference distillation failed: %s. Using raw query.", exc)
        distilled = raw_text.rstrip(".")

    if not distilled or distilled.upper() == "NONE":
        logger.info("[Memory Task] Query did not contain a storable preference. Skipped.")
        return

    try:
        embeddings = generate_surreal_embeddings([distilled], model_name="text-embedding-004")
        vector = embeddings[0]
    except Exception as exc:
        logger.exception("[Memory Task] Failed to generate embedding for distilled preference: %s", exc)
        return

    try:
        surreal_db.add_user_memory(str(user.id), distilled, vector)
        logger.info("[Memory Task] Memory indexed in SurrealDB for user %s: '%s'", user.username, distilled)
    except Exception as s_err:
        logger.warning("[Memory Task] Failed to index memory in SurrealDB: %s", s_err)
