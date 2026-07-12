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
from datetime import timedelta
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.utils import timezone

logger = logging.getLogger(__name__)

from extractor.models import AuditAction, SourceDocument
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


def _fail_document(document_id: int, error_message: str, details: str, log_audit: bool = True) -> None:
    """
    Handles pipeline failures atomically: sets status=FAILED, saves error log,
    optionally writes AuditLog, and broadcasts the status change over Supabase Realtime.
    """
    with transaction.atomic():
        try:
            doc_ref = SourceDocument.objects.select_for_update().get(id=document_id)
        except SourceDocument.DoesNotExist:
            logger.error("[Worker] Cannot fail document %s — does not exist.", document_id)
            return

        doc_ref.status = "FAILED"
        doc_ref.error_message = error_message
        doc_ref.save()

        if log_audit:
            log_audit_event(
                action=AuditAction.EXTRACTION_FAILED,
                user=doc_ref.uploaded_by,
                document=doc_ref,
                details=details,
            )

    # Broadcast failure outside transaction (Gap D-7)
    try:
        broadcast_status_change(str(doc_ref.uuid), "FAILED")
    except Exception as exc:
        logger.debug("[Worker] Failed to broadcast status failure: %s", exc)


def _handle_stage_failure(document_id: int, stage_name: str, exception: Exception) -> None:
    """Centralised failure handler — logs traceback and delegates to _fail_document."""
    err_msg = traceback.format_exc()
    logger.exception("[Worker] Exception in %s: %s", stage_name, err_msg)
    _fail_document(
        document_id,
        error_message=f"{stage_name} Failure:\n{err_msg}",
        details=f"{stage_name} failed: {exception!s}",
    )


def _prepare_document_for_processing(document_id: int) -> SourceDocument | None:
    """
    Lock document row and transition status to EXTRACTING.
    Returns None if document is already finalised, doesn't exist, or budget is exceeded.
    """
    with transaction.atomic():
        try:
            doc = SourceDocument.objects.select_for_update().get(id=document_id)
            if doc.status in ["COMPLETED", "FAILED"]:
                logger.info("[Worker] Document %s already finalised. Skipping.", document_id)
                return None
        except SourceDocument.DoesNotExist:
            logger.error("[Worker] Document %s does not exist.", document_id)
            return None

    try:
        check_budget_and_api_limit()
    except Exception as budget_err:
        _fail_document(
            document_id,
            error_message=f"Budget Capped Halt: {budget_err!s}",
            details=f"Pipeline halted before start due to budget breach: {budget_err!s}",
        )
        return None

    with transaction.atomic():
        doc = SourceDocument.objects.select_for_update().get(id=document_id)
        doc.status = "EXTRACTING"
        doc.save()

        log_audit_event(
            action=AuditAction.EXTRACTION_START,
            user=doc.uploaded_by,
            document=doc,
            details=f"Background curation pipeline started for '{doc.original_filename}' (ID: {doc.id}).",
        )

    broadcast_status_change(str(doc.uuid), "EXTRACTING")
    return doc


def _get_working_path(doc: SourceDocument) -> tuple[str, str | None]:
    """
    Get a local file path for processing.
    For GCS-backed files, streams content in chunks to /tmp to avoid RAM spikes (Gap E-19).
    Returns (working_path, temp_path_or_None).
    """
    temp_local_path = None
    try:
        working_path = doc.file.path
        if not os.path.exists(working_path):
            raise FileNotFoundError("Local file missing")
    except (NotImplementedError, AttributeError, FileNotFoundError):
        # GCS-backed storage: stream file to a local temp file.
        suffix = os.path.splitext(doc.original_filename)[1]
        temp_file_obj = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)
        temp_local_path = temp_file_obj.name
        temp_file_obj.close()
        try:
            # Gap E-19: stream in 64KB chunks to prevent Cloud Run OOM
            doc.file.open("rb")
            with open(temp_local_path, "wb") as out_f:
                for chunk in iter(lambda: doc.file.read(65536), b""):
                    out_f.write(chunk)
            doc.file.close()
        except FileNotFoundError as gcs_err:
            # The GCS object referenced by doc.file does not exist in the bucket.
            # Clean up the empty temp file and propagate a clear error.
            try:
                os.unlink(temp_local_path)
            except OSError:
                pass
            raise FileNotFoundError(
                f"Source file '{doc.file.name}' not found in GCS storage. "
                f"The file may have been deleted or the upload did not complete. "
                f"Original error: {gcs_err}"
            ) from gcs_err
        working_path = temp_local_path
    return working_path, temp_local_path


def _run_stage1(working_path: str, document_id: int) -> SourceDocument:
    """Stage 1: OCR / local parsing."""
    doc = SourceDocument.objects.get(id=document_id)
    lower_name = doc.original_filename.lower()
    raw_markdown = ""
    stage1_cost = Decimal("0.0")
    stage1_input_tokens = 0
    stage1_output_tokens = 0

    if lower_name.endswith(".csv"):
        logger.info("[Worker] Routing CSV to local parser for Document ID: %s", doc.id)
        raw_markdown = process_csv_local(working_path)
        doc_type_detected = "CSV"
    elif lower_name.endswith(".txt"):
        logger.info("[Worker] Routing TXT to local parser for Document ID: %s", doc.id)
        raw_markdown = process_txt_local(working_path)
        doc_type_detected = "TXT"
    else:
        logger.info("[Worker] Routing to Gemini Multimodal OCR for Document ID: %s", doc.id)
        doc_type_detected = "PDF" if lower_name.endswith(".pdf") else "IMAGE"
        ocr_results = run_stage1_multimodal_ocr(working_path, model_name=settings.GEMINI_MODEL)
        raw_markdown = ocr_results["raw_markdown"]
        stage1_cost = ocr_results["cost_usd"]
        stage1_input_tokens = ocr_results["input_tokens"]
        stage1_output_tokens = ocr_results["output_tokens"]

    with transaction.atomic():
        doc_ref = SourceDocument.objects.select_for_update().get(id=document_id)
        doc_ref.document_type = doc_type_detected
        doc_ref.raw_markdown = raw_markdown
        doc_ref.input_tokens += stage1_input_tokens
        doc_ref.output_tokens += stage1_output_tokens
        doc_ref.cost_usd += Decimal(str(stage1_cost))
        doc_ref.status = "REFINING"
        doc_ref.save()

    broadcast_status_change(str(doc_ref.uuid), "REFINING")
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
    line_re = re.compile(r"^(\s*[\w_]+\s*:\s)(.+)$")
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


def _parse_yaml_metadata(
    yaml_metadata_block: str,
    default_title: str | None,
    default_author: str | None,
    default_lang: str | None,
    default_doc_type: str | None,
) -> tuple[str | None, str | None, str | None, str | None, str | None, str | None, str | None, str | None]:
    """Parse YAML metadata block, fallback to defaults on error."""
    import yaml

    parsed_title = default_title
    parsed_author = default_author
    parsed_lang = default_lang
    parsed_doc_type = default_doc_type
    parsed_sig = None
    parsed_isbn = None
    parsed_source_link = None
    parsed_translator = None

    if not yaml_metadata_block:
        return (
            parsed_title,
            parsed_author,
            parsed_lang,
            parsed_doc_type,
            parsed_sig,
            parsed_isbn,
            parsed_source_link,
            parsed_translator,
        )

    for attempt, block in enumerate([yaml_metadata_block, _sanitise_yaml_block(yaml_metadata_block)]):
        try:
            meta_raw = yaml.safe_load(block)
            if not isinstance(meta_raw, dict):
                raise ValueError("YAML metadata block is not a dictionary")
            meta = {str(k).strip().lower(): v for k, v in meta_raw.items()}

            def clean_val(val):
                if val is None:
                    return None
                s = str(val).strip()
                if s.lower() in ("unknown", "n/a", "none", "null", "undefined", ""):
                    return None
                return s

            parsed_title = clean_val(meta.get("title")) or parsed_title
            parsed_author = clean_val(meta.get("author")) or parsed_author
            parsed_lang = clean_val(meta.get("language")) or parsed_lang
            parsed_doc_type = clean_val(meta.get("document_type")) or parsed_doc_type
            parsed_sig = clean_val(meta.get("semantic_signature"))
            parsed_isbn = clean_val(meta.get("isbn"))
            parsed_source_link = clean_val(meta.get("source_link"))
            parsed_translator = clean_val(meta.get("translator"))
            break
        except Exception as yaml_err:
            if attempt == 0:
                logger.debug("[Worker] YAML parse failed on raw block, retrying with sanitiser: %s", yaml_err)
            else:
                logger.warning("[Worker] Metadata YAML parsing failed (using defaults): %s", yaml_err)

    return (
        parsed_title,
        parsed_author,
        parsed_lang,
        parsed_doc_type,
        parsed_sig,
        parsed_isbn,
        parsed_source_link,
        parsed_translator,
    )


# Maximum characters to send to Stage 2 in a single LLM call.
# gemini-2.5-flash has a 1M token window (~4 chars/token) = ~4M chars safe.
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


def _run_stage2(raw_markdown: str, document_id: int) -> SourceDocument:
    """Stage 2: Editorial reasoning refinement.

    For very large documents (e.g. the full Quran, large textbooks), the raw
    markdown may exceed practical LLM context. We split into chunks of at most
    _STAGE2_CHUNK_CHARS characters, refine each chunk separately, then merge:
      - YAML metadata is taken from the FIRST chunk only (contains title/author).
      - Q&A pairs are accumulated across all chunks (up to 20 total).
      - Refined text sections are joined with a section divider.
    Token counts and cost are summed across all chunks.
    """
    doc = SourceDocument.objects.get(id=document_id)
    logger.info("[Worker] Launching Stage 2 Refinement for Document ID: %s", doc.id)

    try:
        from extractor.models import SystemSettings

        settings_obj = SystemSettings.get_settings()
        selected_model = settings_obj.selected_model
    except Exception:
        selected_model = "auto"

    chunks = _split_markdown_into_chunks(raw_markdown)
    total_chunks = len(chunks)

    if total_chunks > 1:
        logger.info(
            "[Worker] Document ID %s is large (%d chars). Splitting Stage 2 into %d chunks.",
            document_id,
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
        logger.info("[Worker] Stage 2 chunk %d/%d for Document ID %s", idx, total_chunks, document_id)
        try:
            chunk_results = run_stage2_editorial_refinement(chunk, model_name=selected_model)
        except Exception as chunk_err:
            logger.error(
                "[Worker] Stage 2 chunk %d/%d failed for Document ID %s: %s", idx, total_chunks, document_id, chunk_err
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
        parsed_isbn,
        parsed_source_link,
        parsed_translator,
    ) = _parse_yaml_metadata(yaml_metadata_block, doc.title, doc.author, doc.language, doc.document_type)

    with transaction.atomic():
        doc_ref = SourceDocument.objects.select_for_update().get(id=document_id)
        doc_ref.refined_markdown = refined_markdown
        doc_ref.yaml_metadata = yaml_metadata_block
        doc_ref.qa_dataset = qa_dataset

        # Ensure we have clean title, author and language values
        def is_unknown(val):
            return not val or str(val).strip().lower() in ("unknown", "n/a", "none", "null", "undefined", "")

        t_val = _truncate(parsed_title, _MAX_TITLE_LEN)
        if is_unknown(t_val):
            import os

            t_val = os.path.splitext(doc_ref.original_filename)[0].replace("_", " ").replace("-", " ").strip()
        doc_ref.title = t_val or doc_ref.original_filename

        a_val = _truncate(parsed_author, _MAX_AUTHOR_LEN)
        if is_unknown(a_val):
            a_val = "Anonymous"

        # Quran translation translation-specific author / publisher corrections
        low_filename = doc_ref.original_filename.lower()
        if "sahih" in low_filename:
            if a_val == "Anonymous" or "divinely" in a_val.lower() or "anonymous" in a_val.lower():
                a_val = "Sahih International"

        doc_ref.author = a_val

        l_val = _truncate(parsed_lang, _MAX_LANGUAGE_LEN)
        if is_unknown(l_val):
            l_val = "English"
        doc_ref.language = l_val

        if parsed_doc_type:
            doc_ref.document_type = _truncate(parsed_doc_type, _MAX_DOCTYPE_LEN)
        if parsed_sig:
            doc_ref.semantic_signature = _truncate(parsed_sig, _MAX_SIG_LEN)

        doc_ref.input_tokens += stage2_input_tokens
        doc_ref.output_tokens += stage2_output_tokens
        doc_ref.cost_usd += Decimal(str(stage2_cost))
        doc_ref.status = "EMBEDDING"
        doc_ref.save()

    broadcast_status_change(str(doc_ref.uuid), "EMBEDDING")
    return doc_ref


def _run_stage3(text_for_chunks: str, document_id: int) -> SourceDocument:
    """
    Stage 3: Semantic chunking and SurrealDB HNSW vector embedding.
    Replaces pgvector DocumentChunk.recreate_chunks with surreal_db.recreate_chunks.
    """
    from extractor import surreal_db

    doc = SourceDocument.objects.get(id=document_id)
    logger.info("[Worker] Segmenting markdown for Document ID: %s", doc.id)

    # Arabic diacritics require smaller chunk sizes for accurate similarity matching (Gap H-3)
    lang = (doc.language or "").lower()
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
                "language": doc.language or "",
                "embedding": emb,
            }
            for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings))
        ]

        # Gap B-8: delete old SurrealDB chunks first, then insert new ones atomically
        surreal_db.recreate_chunks(str(doc.uuid), chunk_payloads)

        # Save chunk payloads permanently to storage (GCS/Local) for stateless sync
        try:
            import json

            from django.core.files.base import ContentFile
            from django.core.files.storage import default_storage

            chunks_json_path = f"chunks/{doc.uuid}.json"
            if default_storage.exists(chunks_json_path):
                default_storage.delete(chunks_json_path)
            default_storage.save(chunks_json_path, ContentFile(json.dumps(chunk_payloads).encode("utf-8")))
            logger.info("[Worker] Chunks and embeddings uploaded to persistent storage for doc: %s", doc.uuid)
        except Exception as exc:
            logger.warning("[Worker] Failed to save chunks to persistent storage: %s", exc)

        with transaction.atomic():
            doc_ref = SourceDocument.objects.select_for_update().get(id=document_id)
            doc_ref.page_count = len(chunks)
            doc_ref.status = "COMPLETED"
            doc_ref.expires_at = timezone.now() + timedelta(days=retention_days)
            doc_ref.save()
    else:
        # No chunks — mark completed with empty vector store
        surreal_db.delete_chunks(str(doc.uuid))

        with transaction.atomic():
            doc_ref = SourceDocument.objects.select_for_update().get(id=document_id)
            doc_ref.page_count = 0
            doc_ref.status = "COMPLETED"
            doc_ref.expires_at = timezone.now() + timedelta(days=retention_days)
            doc_ref.save()

    broadcast_status_change(str(doc_ref.uuid), "COMPLETED")
    return doc_ref


def _run_pipeline_stages(initial_doc: SourceDocument, working_path: str, document_id: int) -> bool:
    logger.info("[Worker] Running pipeline stages for document: %s", initial_doc.original_filename)
    # Stage 1
    try:
        doc = _run_stage1(working_path, document_id)
    except Exception as exc:
        _handle_stage_failure(document_id, "Stage 1", exc)
        return False

    # Mid-pipeline budget circuit breaker
    try:
        check_budget_and_api_limit()
    except Exception as budget_err:
        logger.warning("[Worker] Mid-pipeline budget limit breached: %s", budget_err)
        _fail_document(
            document_id,
            error_message=f"Mid-Pipeline Budget Capped Halt: {budget_err!s}",
            details=f"Mid-pipeline budget breach: {budget_err!s}",
        )
        return False

    # Stage 2
    try:
        doc = _run_stage2(doc.raw_markdown, document_id)
    except Exception as exc:
        _handle_stage_failure(document_id, "Stage 2", exc)
        return False

    # Stage 3
    try:
        text_for_chunks = doc.refined_markdown or doc.raw_markdown
        doc = _run_stage3(text_for_chunks, document_id)
        doc.refresh_from_db()

        logger.info("[Worker] Pipeline completed successfully for ID %s!", document_id)
        log_audit_event(
            action=AuditAction.EXTRACTION_COMPLETED,
            user=doc.uploaded_by,
            document=doc,
            details=(
                f"Curation pipeline completed. Pages: {doc.page_count}. "
                f"Cost: ${doc.cost_usd:.6f} USD. "
                f"Tokens in: {doc.input_tokens}, out: {doc.output_tokens}."
            ),
        )
        return True
    except Exception as exc:
        _handle_stage_failure(document_id, "Stage 3", exc)
        return False


def process_document_task(payload: dict) -> None:
    """
    Main pipeline handler: OCR → Refinement → Embedding.
    Receives a Cloud Tasks payload dict with document_id.
    """
    document_id = payload.get("document_id")
    if not document_id:
        logger.error("[Worker] process_document_task called with missing document_id")
        return

    logger.info("[Worker] Starting processing pipeline for Document ID: %s", document_id)
    try:
        doc = _prepare_document_for_processing(document_id)
        if not doc:
            return

        temp_local_path = None
        try:
            working_path, temp_local_path = _get_working_path(doc)
            _run_pipeline_stages(doc, working_path, document_id)
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
                document_id,
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
    document_id = payload.get("document_id")
    if not document_id:
        logger.error("[Worker] reembed_edited_document_task called with missing document_id")
        return

    from extractor import surreal_db

    logger.info("[Worker] Re-embedding Document ID: %s", document_id)

    with transaction.atomic():
        try:
            doc = SourceDocument.objects.select_for_update().get(id=document_id)
            doc.status = "EMBEDDING"
            doc.save()
        except SourceDocument.DoesNotExist:
            logger.error("[Worker] Document %s does not exist.", document_id)
            return

    broadcast_status_change(str(doc.uuid), "EMBEDDING")

    try:
        text_for_chunks = doc.refined_markdown or doc.raw_markdown
        lang = (doc.language or "").lower()
        chunk_size = 500 if "arabic" in lang or "ar" in lang else 1200
        chunks = chunk_document_semantically(text_for_chunks, max_chunk_size=chunk_size)

        # Gap B-8: always purge old SurrealDB chunks on re-embed
        surreal_db.delete_chunks(str(doc.uuid))

        if chunks:
            check_budget_and_api_limit()
            embeddings = generate_surreal_embeddings(chunks, model_name="text-embedding-004")
            chunk_payloads = [
                {
                    "chunk_index": i,
                    "content": ct,
                    "token_count": len(ct.split()),
                    "language": doc.language or "",
                    "embedding": emb,
                }
                for i, (ct, emb) in enumerate(zip(chunks, embeddings))
            ]
            surreal_db.recreate_chunks(str(doc.uuid), chunk_payloads)

            with transaction.atomic():
                doc = SourceDocument.objects.select_for_update().get(id=document_id)
                doc.page_count = len(chunks)
                doc.status = "COMPLETED"
                doc.save()
        else:
            with transaction.atomic():
                doc = SourceDocument.objects.select_for_update().get(id=document_id)
                doc.page_count = 0
                doc.status = "COMPLETED"
                doc.save()

        logger.info("[Worker] Re-embedding successful for Document ID: %s!", doc.id)
        broadcast_status_change(str(doc.uuid), "COMPLETED")

    except Exception as exc:
        err_msg = traceback.format_exc()
        logger.exception("[Worker] Exception in re-embedding: %s", err_msg)
        _fail_document(
            document_id,
            error_message=f"Re-embedding Failure:\n{err_msg}",
            details=f"Re-embedding failed: {exc!s}",
            log_audit=False,
        )


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

    expired_docs = SourceDocument.objects.filter(expires_at__lte=now)
    purged_count = 0

    for doc in expired_docs:
        file_hash = doc.file_hash
        doc_uuid = str(doc.uuid)
        shared_references = SourceDocument.objects.filter(file_hash=file_hash).exclude(id=doc.id).count()

        if shared_references == 0:
            logger.info("[Cron] Purging file hash %s from storage.", file_hash)
            try:
                doc.file.delete(save=False)
            except Exception as exc:
                logger.warning("[Cron] Failed to delete physical file for hash %s: %s", file_hash, exc)
        else:
            logger.info(
                "[Cron] Skipping physical delete for hash %s (referenced by %s records).", file_hash, shared_references
            )

        # Gap B-8: cascade delete SurrealDB chunks and storage JSON backups for compliance
        try:
            surreal_db.delete_chunks(doc_uuid)
        except Exception as exc:
            logger.warning("[Cron] Failed to delete SurrealDB chunks for %s: %s", doc_uuid, exc)

        try:
            from django.core.files.storage import default_storage

            chunks_json_path = f"chunks/{doc_uuid}.json"
            if default_storage.exists(chunks_json_path):
                default_storage.delete(chunks_json_path)
        except Exception as storage_err:
            logger.warning("[Cron] Failed to delete storage chunk JSON for %s: %s", doc_uuid, storage_err)

        # Gap E-30: write audit log before deletion
        log_audit_event(
            action=AuditAction.DELETE,
            user=None,
            document=doc,
            details=f"GDPR retention cleanup: document '{doc.original_filename}' (UUID: {doc_uuid}) expired and purged.",
        )

        doc.delete()
        purged_count += 1

    # Gap B-9: purge expired SurrealDB RAG cache entries
    try:
        pruned = surreal_db.purge_expired_rag_cache()
        if pruned:
            logger.info("[Cron] Pruned %s expired SurrealDB RAG cache entries.", pruned)
    except Exception as exc:
        logger.warning("[Cron] Failed to purge expired RAG cache: %s", exc)

    logger.info("[Cron] Cleanup finished. Deleted %s expired records.", purged_count)


def reap_stale_tasks(_payload: dict | None = None) -> int:
    """
    Marks documents stuck in transient states for >15 minutes as FAILED.
    Gap E-31: writes audit entries and broadcasts status updates for reaped tasks.
    """
    logger.info("[Reaper] Scanning for stale active tasks...")
    stale_threshold = timezone.now() - timezone.timedelta(minutes=15)
    stale_docs = SourceDocument.objects.filter(
        status__in=["EXTRACTING", "REFINING", "EMBEDDING"], updated_at__lte=stale_threshold
    )
    reaped_count = 0

    for doc in stale_docs:
        with transaction.atomic():
            try:
                doc_ref = SourceDocument.objects.select_for_update().get(id=doc.id)
            except SourceDocument.DoesNotExist:
                continue

            if doc_ref.status in ["EXTRACTING", "REFINING", "EMBEDDING"] and doc_ref.updated_at <= stale_threshold:
                doc_ref.status = "FAILED"
                doc_ref.error_message = (
                    "Task terminated unexpectedly. "
                    "The background worker may have scaled down, been preempted, or restarted."
                )
                doc_ref.save()
                reaped_count += 1
                logger.warning("[Reaper] Reaped stale document task %s (was %s).", doc_ref.id, doc.status)

                # Gap E-31: write audit log for reaped task
                log_audit_event(
                    action=AuditAction.EXTRACTION_FAILED,
                    user=doc_ref.uploaded_by,
                    document=doc_ref,
                    details=(
                        f"[Reaper] Document '{doc_ref.original_filename}' was stuck in '{doc.status}' for >15 minutes "
                        "and has been automatically marked as FAILED."
                    ),
                )

                # Gap E-31: broadcast status update so dashboard updates without hard reload
                try:
                    broadcast_status_change(str(doc_ref.uuid), "FAILED")
                except Exception as exc:
                    logger.debug("[Reaper] Failed to broadcast status failure: %s", exc)

    if reaped_count > 0:
        logger.info("[Reaper] Successfully reaped %s stuck tasks.", reaped_count)

    return reaped_count


def store_user_memory_task(payload: dict) -> None:
    """
    Cloud Tasks receiver: Distill a user preference statement, generate its embedding,
    persist it to PostgreSQL (Supabase), and index it in SurrealDB.
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
    from extractor.models import UserMemory
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
    model = _resolve_model_name("google/gemini-2.5-flash-lite")

    try:
        response, _ = execute_generate_content_with_fallback(client, model, contents=[distill_prompt])
        distilled = response.text.strip().strip("\"'")
    except Exception as exc:
        logger.warning("[Memory Task] Gemini preference distillation failed: %s. Using raw query.", exc)
        distilled = raw_text

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
        UserMemory.objects.create(user=user, memory_text=distilled, embedding=vector)
        logger.info("[Memory Task] Persistent memory created for user %s: '%s'", user.username, distilled)
    except Exception as db_err:
        logger.exception("[Memory Task] Failed to write memory to PostgreSQL: %s", db_err)

    try:
        surreal_db.add_user_memory(str(user.id), distilled, vector)
        logger.info("[Memory Task] Memory indexed in SurrealDB for user %s.", user.username)
    except Exception as s_err:
        logger.warning("[Memory Task] Failed to index memory in SurrealDB: %s", s_err)
