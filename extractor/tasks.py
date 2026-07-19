"""
Async Worker Tasks — AetherOmni v2.0

Pipeline: Stage 1 (Gemini Multimodal OCR) → Stage 2 (Editorial Refinement)
          → Stage 3 (SurrealDB Semantic Chunking + Vector Embeddings)

All task functions receive a `payload` dict (as dispatched by cloud_tasks.enqueue).
The task_handlers.TASK_REGISTRY maps task_name → function.
"""

import logging
import os
import tempfile
import traceback
from datetime import UTC, timedelta
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.core.files.storage import default_storage
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from extractor import surreal_db
from extractor.models import AuditAction
from extractor.utils import (
    broadcast_status_change,
    check_budget_and_api_limit,
    chunk_document_semantically,
    generate_surreal_embeddings,
    log_audit_event,
    process_csv_local,
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


def _determine_actual_page_count(working_path: str, doc_type: str) -> int:
    """Helper to parse the PDF file structures and extract page count using regex without loading the entire file into memory."""
    if doc_type != "PDF":
        return 1
    try:
        import re

        chunk_size = 128 * 1024
        overlap = 1024
        pages_count = 0
        parent_count = 0

        pages_pattern = re.compile(rb"/Type\s*/Page\b")
        parent_pattern = re.compile(rb"/Parent\s*\d+\s*\d+\s*R")
        count_pattern = re.compile(rb"/Type\s*/Pages.*?/Count\s*(\d+)", re.DOTALL)

        # Check for small or empty/dummy file first
        with open(working_path, "rb") as f:
            header = f.read(1024)
            if b"Dummy PDF Content" in header:
                return 1

        # We first do a pass to count occurrences of Page and Parent.
        with open(working_path, "rb") as f:
            buffer = b""
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                content = buffer + chunk
                pages_count += len(pages_pattern.findall(content))
                parent_count += len(parent_pattern.findall(content))
                
                # Keep overlap in buffer
                if len(content) > overlap:
                    buffer = content[-overlap:]
                else:
                    buffer = content

        if pages_count > 0:
            return pages_count

        # If no /Type /Page found, search for the count pattern
        with open(working_path, "rb") as f:
            buffer = b""
            while True:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                content = buffer + chunk
                match = count_pattern.search(content)
                if match:
                    return int(match.group(1))
                if len(content) > overlap:
                    buffer = content[-overlap:]
                else:
                    buffer = content

        if parent_count > 0:
            return parent_count

    except Exception as exc:
        logger.warning("[Worker] Failed to determine PDF page count: %s", exc)
    return 1



# Maximum field lengths — prevents Cloud Run OOM and DB varchar crashes (Gap E-17)
_MAX_TITLE_LEN = 255
_MAX_AUTHOR_LEN = 255
_MAX_LANGUAGE_LEN = 50
_MAX_DOCTYPE_LEN = 50
_MAX_SIG_LEN = 64


def _truncate(value: str | None, max_len: int) -> str:
    """Safely truncate a string to the maximum database column length."""
    if not value:
        return ""
    return value[:max_len]


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
        from django.contrib.auth import get_user_model

        User = get_user_model()
        uploaded_by_id = doc.get("uploaded_by_id")
        user = User.objects.filter(id=uploaded_by_id).first() if uploaded_by_id else None
        log_audit_event(
            action=AuditAction.EXTRACTION_FAILED,
            user=user,
            document=doc,
            details=details,
        )

    # Broadcast failure outside transaction (Gap D-7)
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
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        logger.error("[Worker] Document %s does not exist.", doc_uuid)
        return None
    if doc.get("status") in ["COMPLETED", "FAILED"]:
        logger.info("[Worker] Document %s already finalised. Skipping.", doc_uuid)
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

    doc = surreal_db.update_document(doc_uuid, {"status": "EXTRACTING"})

    from django.contrib.auth import get_user_model

    User = get_user_model()
    uploaded_by_id = doc.get("uploaded_by_id")
    user = User.objects.filter(id=uploaded_by_id).first() if uploaded_by_id else None

    log_audit_event(
        action=AuditAction.EXTRACTION_START,
        user=user,
        document=doc,
        details=f"Background curation pipeline started for '{doc.get('original_filename')}' (UUID: {doc_uuid}).",
    )

    broadcast_status_change(doc_uuid, "EXTRACTING")
    return doc


def _get_working_path(doc: dict) -> tuple[str, str | None]:
    """
    Get a local file path for processing.
    For GCS-backed files, streams content in chunks to /tmp to avoid RAM spikes (Gap E-19).
    Returns (working_path, temp_path_or_None).
    """
    temp_local_path = None
    if isinstance(doc, dict):
        file_rel_path = doc.get("file", "")
        orig_filename = doc.get("original_filename", "")
    else:
        file_rel_path = getattr(doc, "file", "")
        if hasattr(file_rel_path, "name"):
            file_rel_path = file_rel_path.name
        elif file_rel_path and not isinstance(file_rel_path, str):
            file_rel_path = str(file_rel_path)
        orig_filename = getattr(doc, "original_filename", "")
    try:
        working_path = default_storage.path(file_rel_path)
        if os.path.exists(working_path):
            return working_path, None
    except (NotImplementedError, AttributeError):
        pass

    # GCS-backed storage: stream file to a local temp file.
    suffix = os.path.splitext(orig_filename)[1]
    temp_file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
    temp_local_path = temp_file_obj.name
    temp_file_obj.close()
    try:
        # Gap E-19: stream in 64KB chunks to prevent Cloud Run OOM
        if isinstance(doc, dict):
            with default_storage.open(file_rel_path, "rb") as in_f:
                with open(temp_local_path, "wb") as out_f:
                    for chunk in iter(lambda: in_f.read(65536), b""):
                        out_f.write(chunk)
        else:
            doc.file.open("rb")
            try:
                with open(temp_local_path, "wb") as out_f:
                    for chunk in iter(lambda: doc.file.read(65536), b""):
                        out_f.write(chunk)
            finally:
                doc.file.close()
    except Exception as gcs_err:
        try:
            os.unlink(temp_local_path)
        except OSError:
            pass
        raise FileNotFoundError(
            f"Source file '{file_rel_path}' not found in GCS storage. "
            f"The file may have been deleted or the upload did not complete. "
            f"Original error: {gcs_err}"
        ) from gcs_err

    return temp_local_path, temp_local_path


def _run_stage1(working_path: str, document_id: str | int) -> Any:
    """Stage 1: OCR / local parsing."""
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
        except (SourceDocument.DoesNotExist, ValueError) as err:
            logger.error("[Worker] Document ID/UUID %s not found in SQLite: %s", document_id, err)
            raise
        lower_name = doc.original_filename.lower()
        doc_id_display = doc.id
    else:
        doc = surreal_db.get_document(str(document_id))
        if not doc:
            raise ValueError(f"Document {document_id} not found in SurrealDB")
        lower_name = doc.get("original_filename", "").lower()
        doc_id_display = doc.get("doc_uuid")

    raw_markdown = ""
    stage1_cost = Decimal("0.0")
    stage1_input_tokens = 0
    stage1_output_tokens = 0

    if lower_name.endswith(".csv"):
        logger.info("[Worker] Routing CSV to local parser for Document ID: %s", doc_id_display)
        raw_markdown = process_csv_local(working_path)
        doc_type_detected = "CSV"
    elif lower_name.endswith(".txt"):
        logger.info("[Worker] Routing TXT to local parser for Document ID: %s", doc_id_display)
        raw_markdown = process_txt_local(working_path)
        doc_type_detected = "TXT"
    else:
        logger.info("[Worker] Routing to Gemini Multimodal OCR for Document ID: %s", doc_id_display)
        doc_type_detected = "PDF" if lower_name.endswith(".pdf") else "IMAGE"
        ocr_results = run_stage1_multimodal_ocr(working_path, model_name=settings.GEMINI_MODEL)
        raw_markdown = ocr_results["raw_markdown"]
        stage1_cost = ocr_results["cost_usd"]
        stage1_input_tokens = ocr_results["input_tokens"]
        stage1_output_tokens = ocr_results["output_tokens"]

    page_count_detected = _determine_actual_page_count(working_path, doc_type_detected)

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
            doc_ref.cost_usd += Decimal(str(stage1_cost))
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
            "cost_usd": float(current_cost) + float(stage1_cost),
            "status": "REFINING",
        }
        doc_ref = surreal_db.update_document(str(document_id), updated_data)
        broadcast_status_change(str(document_id), "REFINING")
        return doc_ref


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


def _extract_meta_dict(meta, default_title, default_author, default_lang, default_doc_type):
    parsed_title = _clean_val(meta.get("title")) or default_title
    parsed_author = _clean_val(meta.get("author")) or default_author
    parsed_lang = _clean_val(meta.get("language")) or default_lang
    parsed_doc_type = _clean_val(meta.get("document_type")) or default_doc_type
    parsed_sig = _clean_val(meta.get("semantic_signature"))
    parsed_isbn = _clean_val(meta.get("isbn"))
    parsed_source_link = _clean_val(meta.get("source_link"))
    parsed_translator = _clean_val(meta.get("translator"))

    parsed_pub = _clean_val(meta.get("publisher"))
    parsed_year = _clean_val(meta.get("publication_year"))
    parsed_lic = _clean_val(meta.get("license_type"))
    parsed_doi = _clean_val(meta.get("doi"))

    return (
        parsed_title,
        parsed_author,
        parsed_lang,
        parsed_doc_type,
        parsed_sig,
        parsed_isbn,
        parsed_source_link,
        parsed_translator,
        parsed_pub,
        parsed_year,
        parsed_lic,
        parsed_doi,
    )


def _parse_yaml_metadata(
    yaml_metadata_block: str,
    default_title: str | None,
    default_author: str | None,
    default_lang: str | None,
    default_doc_type: str | None,
) -> tuple:
    """Parse YAML metadata block, fallback to defaults on error."""
    import yaml

    parsed = (
        default_title,
        default_author,
        default_lang,
        default_doc_type,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
        None,
    )

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
# gemini-3.5-flash has a 1M token window (~4 chars/token) = ~4M chars safe.
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


def _update_doc_metadata(
    doc_ref,
    parsed_title,
    parsed_author,
    parsed_lang,
    parsed_doc_type,
    parsed_sig,
    parsed_pub=None,
    parsed_year=None,
    parsed_lic=None,
    parsed_doi=None,
):
    """Apply parsed YAML metadata values to a SourceDocument instance or dictionary (inside atomic block)."""
    import os

    orig_filename = _get_val(doc_ref, "original_filename", "")
    t_val = _truncate(parsed_title, _MAX_TITLE_LEN)
    if _is_unknown_value(t_val):
        t_val = os.path.splitext(orig_filename)[0].replace("_", " ").replace("-", " ").strip()
    _set_val(doc_ref, "title", t_val or orig_filename or "Untitled")

    a_val = _truncate(parsed_author, _MAX_AUTHOR_LEN)
    if _is_unknown_value(a_val):
        a_val = "Anonymous"

    # Quran translation-specific author / publisher corrections
    low_filename = orig_filename.lower()
    if "sahih" in low_filename:
        if a_val == "Anonymous" or "divinely" in a_val.lower() or "anonymous" in a_val.lower():
            a_val = "Sahih International"

    _set_val(doc_ref, "author", a_val)

    l_val = _truncate(parsed_lang, _MAX_LANGUAGE_LEN)
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
        clean_doi = str(parsed_doi).replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
        _set_val(doc_ref, "doi", _truncate(clean_doi, 255))


def _run_stage2(raw_markdown: str, doc_uuid: str) -> dict:
    """Stage 2: Editorial reasoning refinement.

    For very large documents (e.g. the full Quran, large textbooks), the raw
    markdown may exceed practical LLM context. We split into chunks of at most
    _STAGE2_CHUNK_CHARS characters, refine each chunk separately, then merge:
      - YAML metadata is taken from the FIRST chunk only (contains title/author).
      - Q&A pairs are accumulated across all chunks (up to 20 total).
      - Refined text sections are joined with a section divider.
    Token counts and cost are summed across all chunks.
    """
    doc = surreal_db.get_document(doc_uuid)
    if not doc:
        raise ValueError(f"Document {doc_uuid} not found")
    logger.info("[Worker] Launching Stage 2 Refinement for Document UUID: %s", doc_uuid)

    try:
        settings_obj = surreal_db.get_system_settings()
        selected_model = settings_obj.get("selected_model", "auto")
    except Exception:
        selected_model = "auto"

    chunks = _split_markdown_into_chunks(raw_markdown)
    total_chunks = len(chunks)

    if total_chunks > 1:
        logger.info(
            "[Worker] Document UUID %s is large (%d chars). Splitting Stage 2 into %d chunks.",
            doc_uuid,
            len(raw_markdown),
            total_chunks,
        )

    refined_parts: list[str] = []
    yaml_metadata_block: str = ""
    qa_dataset: list = []
    stage2_cost = 0.0
    stage2_input_tokens = 0
    stage2_output_tokens = 0

    for idx, chunk in enumerate(chunks, 1):
        logger.info("[Worker] Stage 2 chunk %d/%d for Document UUID %s", idx, total_chunks, doc_uuid)
        try:
            chunk_results = run_stage2_editorial_refinement(chunk, model_name=selected_model)
        except Exception as chunk_err:
            logger.exception(
                "[Worker] Stage 2 chunk %d/%d failed for Document UUID %s: %s", idx, total_chunks, doc_uuid, chunk_err
            )
            # On chunk failure, preserve the raw chunk text so content is not lost
            refined_parts.append(chunk)
            continue

        refined_parts.append(chunk_results["refined_markdown"])

        # Only keep YAML from the first chunk (it has the title/author/language)
        if idx == 1:
            yaml_metadata_block = chunk_results["yaml_metadata"]

        # Accumulate Q&A pairs (cap total at 20)
        for qa in chunk_results.get("qa_dataset", []):
            if len(qa_dataset) < 20:
                qa_dataset.append(qa)

        stage2_cost += float(chunk_results["cost_usd"])
        stage2_input_tokens += chunk_results["input_tokens"]
        stage2_output_tokens += chunk_results["output_tokens"]

    # Merge refined text — join chunks with a light visual separator
    refined_markdown = "\n\n---\n\n".join(p for p in refined_parts if p.strip())

    (
        parsed_title,
        parsed_author,
        parsed_lang,
        parsed_doc_type,
        parsed_sig,
        _parsed_isbn,
        _parsed_source_link,
        _parsed_translator,
        parsed_pub,
        parsed_year,
        parsed_lic,
        parsed_doi,
    ) = _parse_yaml_metadata(
        yaml_metadata_block,
        doc.get("title", ""),
        doc.get("author", ""),
        doc.get("language", ""),
        doc.get("document_type", ""),
    )

    doc_ref = surreal_db.get_document(doc_uuid)
    _update_doc_metadata(
        doc_ref,
        parsed_title,
        parsed_author,
        parsed_lang,
        parsed_doc_type,
        parsed_sig,
        parsed_pub,
        parsed_year,
        parsed_lic,
        parsed_doi,
    )

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

        chunk_payloads = [
            {
                "chunk_index": i,
                "content": chunk_text,
                "token_count": len(chunk_text.split()),
                "language": doc.get("language") or "",
                "embedding": emb,
            }
            for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings))
        ]

        # Gap B-8: delete old SurrealDB chunks first, then insert new ones atomically
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
        final_page_count = existing_page_count if existing_page_count > 0 else 0
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


def _run_pipeline_stages(initial_doc: dict, working_path: str, doc_uuid: str) -> bool:
    from extractor.surreal_db import _model_to_dict

    if not isinstance(initial_doc, dict):
        initial_doc = _model_to_dict(initial_doc)

    logger.info("[Worker] Running pipeline stages for document: %s", initial_doc.get("original_filename"))
    # Stage 1
    try:
        doc = _run_stage1(working_path, doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)
    except Exception as exc:
        _handle_stage_failure(doc_uuid, "Stage 1", exc)
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

    # Stage 2
    try:
        doc = _run_stage2(doc.get("raw_markdown", ""), doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)
    except Exception as exc:
        _handle_stage_failure(doc_uuid, "Stage 2", exc)
        return False

    # Stage 3
    try:
        text_for_chunks = doc.get("refined_markdown") or doc.get("raw_markdown", "")
        doc = _run_stage3(text_for_chunks, doc_uuid)
        if not isinstance(doc, dict):
            doc = _model_to_dict(doc)

        logger.info("[Worker] Pipeline completed successfully for UUID %s!", doc_uuid)

        from django.contrib.auth import get_user_model

        User = get_user_model()
        uploaded_by_id = doc.get("uploaded_by_id")
        user = User.objects.filter(id=uploaded_by_id).first() if uploaded_by_id else None

        log_audit_event(
            action=AuditAction.EXTRACTION_COMPLETED,
            user=user,
            document=doc,
            details=(
                f"Curation pipeline completed. Pages: {doc.get('page_count')}. "
                f"Cost: ${doc.get('cost_usd'):.6f} USD. "
                f"Tokens in: {doc.get('input_tokens')}, out: {doc.get('output_tokens')}."
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
            working_path, temp_local_path = _get_working_path(doc)
            _run_pipeline_stages(doc, working_path, doc_uuid)
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
    Gap B-8: explicitly purges old SurrealDB chunks before inserting new ones.
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

        # Gap B-8: always purge old SurrealDB chunks on re-embed
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
            final_page_count = existing_page_count if existing_page_count > 0 else 0
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


def _cleanup_single_expired_doc(doc: dict, hash_counts: dict, surreal_db):
    """Purge one expired document: physical file, SurrealDB chunks, storage JSON, audit log."""
    file_hash = doc.get("file_hash")
    doc_uuid = doc.get("doc_uuid")
    file_rel_path = doc.get("file", "")

    total_refs = hash_counts.get(file_hash, 0)
    shared_references = max(0, total_refs - 1)

    from django.conf import settings

    doc_obj = None
    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            import uuid

            try:
                uuid.UUID(str(doc_uuid))
                doc_obj = SourceDocument.objects.get(uuid=doc_uuid)
            except ValueError:
                doc_obj = SourceDocument.objects.get(id=int(doc_uuid))
        except (ValueError, SourceDocument.DoesNotExist) as lookup_err:
            logger.debug("[Cron] Could not resolve doc_uuid %s: %s", doc_uuid, lookup_err)

    if shared_references == 0:
        logger.info("[Cron] Purging file hash %s from storage.", file_hash)
        try:
            if doc_obj:
                doc_obj.file.delete(save=False)
            else:
                if default_storage.exists(file_rel_path):
                    default_storage.delete(file_rel_path)
        except Exception as exc:
            logger.warning("[Cron] Failed to delete physical file for hash %s: %s", file_hash, exc)
    else:
        logger.info(
            "[Cron] Skipping physical delete for hash %s (referenced by %s records).", file_hash, shared_references
        )

    if file_hash in hash_counts:
        hash_counts[file_hash] = max(0, hash_counts[file_hash] - 1)

    # Gap B-8: cascade delete SurrealDB chunks for compliance
    try:
        surreal_db.delete_chunks(doc_uuid)
    except Exception as exc:
        logger.warning("[Cron] Failed to delete SurrealDB chunks for %s: %s", doc_uuid, exc)

    # Gap E-30: write audit log before deletion
    log_audit_event(
        action=AuditAction.DELETE,
        user=None,
        document=doc,
        details=f"GDPR retention cleanup: document '{doc.get('original_filename')}' (UUID: {doc_uuid}) expired and purged.",
    )

    surreal_db.delete_document(doc_uuid)


def cleanup_expired_documents_task(_payload: dict | None = None) -> None:
    """
    Reference-counted document garbage disposal.
    Gap B-8: cascades SurrealDB chunk deletions on expiry.
    Gap B-9: purges expired SurrealDB RAG cache entries.
    Gap E-30: writes audit entries for each purged document.
    """
    from extractor import surreal_db

    logger.info("[Cron] Starting reference-counted expired document cleanup...")
    now = timezone.now()
    now_str = format_datetime(now)

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        expired_qs = SourceDocument.objects.filter(expires_at__lte=now)
        expired_docs = [surreal_db._model_to_dict(d) for d in expired_qs]
        if expired_docs:
            expired_hashes = {doc.get("file_hash") for doc in expired_docs if doc.get("file_hash")}
            hash_counts = {}
            for file_hash in expired_hashes:
                hash_counts[file_hash] = SourceDocument.objects.filter(file_hash=file_hash).count()
        else:
            hash_counts = {}
    else:
        expired_sql = "SELECT * FROM documents WHERE expires_at <= <datetime> $now;"
        expired_docs = surreal_db._first_result(surreal_db._run(expired_sql, {"now": now_str}))
        purged_count = 0

        if expired_docs:
            expired_hashes = {doc.get("file_hash") for doc in expired_docs if doc.get("file_hash")}
            hash_counts = {}
            for file_hash in expired_hashes:
                count_sql = "SELECT count() AS n FROM documents WHERE file_hash = $file_hash GROUP ALL;"
                res = surreal_db._first_result(surreal_db._run(count_sql, {"file_hash": file_hash}))
                hash_counts[file_hash] = res[0].get("n", 0) if res else 0
        else:
            hash_counts = {}

    purged_count = 0
    for doc in expired_docs:
        _cleanup_single_expired_doc(doc, hash_counts, surreal_db)
        purged_count += 1

    # Gap B-9: purge expired SurrealDB RAG cache entries
    try:
        pruned = surreal_db.purge_expired_rag_cache()
        if pruned:
            logger.info("[Cron] Pruned %s expired SurrealDB RAG cache entries.", pruned)
    except Exception as exc:
        logger.warning("[Cron] Failed to purge expired RAG cache: %s", exc)

    logger.info("[Cron] Cleanup finished. Deleted %s expired records.", purged_count)


def _reap_single_stale_doc(doc: dict, stale_threshold_str: str) -> bool:
    """Lock and check one stale document; mark FAILED if still stuck. Returns True if reaped."""
    doc_uuid = doc.get("doc_uuid")

    surreal_db.update_document(
        doc_uuid,
        {
            "status": "FAILED",
            "error_message": (
                "Task terminated unexpectedly. "
                "The background worker may have scaled down, been preempted, or restarted."
            ),
        },
    )
    logger.warning("[Reaper] Reaped stale document task %s (was %s).", doc_uuid, doc.get("status"))

    from django.contrib.auth import get_user_model

    User = get_user_model()
    uploaded_by_id = doc.get("uploaded_by_id")
    user = User.objects.filter(id=uploaded_by_id).first() if uploaded_by_id else None

    # Gap E-31: write audit log for reaped task
    log_audit_event(
        action=AuditAction.EXTRACTION_FAILED,
        user=user,
        document=doc,
        details=(
            f"[Reaper] Document '{doc.get('original_filename')}' was stuck in '{doc.get('status')}' for >15 minutes "
            "and has been automatically marked as FAILED."
        ),
    )

    # Gap E-31: broadcast status update so dashboard updates without hard reload
    try:
        broadcast_status_change(doc_uuid, "FAILED")
    except Exception as exc:
        logger.debug("[Reaper] Failed to broadcast status failure: %s", exc)

    return True


def reap_stale_tasks(_payload: dict | None = None) -> int:
    """
    Marks documents stuck in transient states for >15 minutes as FAILED.
    Gap E-31: writes audit entries and broadcasts status updates for reaped tasks.
    """
    logger.info("[Reaper] Scanning for stale active tasks...")
    stale_threshold = timezone.now() - timezone.timedelta(minutes=15)
    stale_threshold_str = format_datetime(stale_threshold)

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        stale_qs = SourceDocument.objects.filter(
            status__in=["EXTRACTING", "REFINING", "EMBEDDING"], updated_at__lte=stale_threshold
        )
        stale_docs = [surreal_db._model_to_dict(d) for d in stale_qs]
    else:
        stale_sql = "SELECT * FROM documents WHERE status INSIDE $states AND updated_at <= <datetime> $threshold;"
        stale_docs = surreal_db._first_result(
            surreal_db._run(
                stale_sql, {"states": ["EXTRACTING", "REFINING", "EMBEDDING"], "threshold": stale_threshold_str}
            )
        )
    reaped_count = 0

    for doc in stale_docs:
        if _reap_single_stale_doc(doc, stale_threshold_str):
            reaped_count += 1

    if reaped_count > 0:
        logger.info("[Reaper] Successfully reaped %s stuck tasks.", reaped_count)

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

    from django.contrib.auth.models import User

    from extractor import surreal_db
    from extractor.llm_gateway import (
        _init_refinement_client,
        _resolve_model_name,
        execute_generate_content_with_fallback,
    )
    from extractor.rag import generate_surreal_embeddings

    try:
        user = User.objects.get(id=user_id)
    except User.DoesNotExist:
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
