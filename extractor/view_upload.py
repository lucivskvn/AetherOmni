import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View

from extractor import surreal_db, view_common
from extractor.models import AuditAction
from extractor.task_state import PENDING_OPERATION
from extractor.utils import (
    VIDEO_FILE_EXTENSIONS,
    broadcast_status_change,
    calculate_file_sha256,
    get_client_ip,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

# Aggregate batch cap: Cloud Run rejects requests over ~32MB before Django
# runs, so an unbounded multi-file POST surfaces as an opaque platform 413.
MAX_UPLOAD_BATCH_SIZE = 20

# NOTE: ".zip" is intentionally absent — validate_zip()/safe_extract() in
# file_ingest.py are currently unwired helpers. If ZIP upload is ever added,
# both guards MUST be applied at extraction time (AUD-380).
ALLOWED_UPLOAD_EXTENSIONS = {
    "pdf",
    "png",
    "jpg",
    "jpeg",
    "webp",
    "gif",
    "tiff",
    "heic",
    "heif",
    "csv",
    "txt",
    "md",
    "markdown",
    "json",
    "docx",
    "doc",
    "xlsx",
    "xls",
    *(ext.lstrip(".") for ext in VIDEO_FILE_EXTENSIONS),
}


def _upload_title_from_filename(orig_name: str) -> str:
    """Return a displayable title derived from a validated uploaded filename."""
    import os

    return os.path.splitext(os.path.basename(orig_name))[0].replace("_", " ").replace("-", " ").strip()


def _validate_upload_file(orig_name: str, size: int) -> dict[str, str] | None:
    import os

    ext = os.path.splitext(orig_name)[1].lower().replace(".", "")
    if not ext or ext not in ALLOWED_UPLOAD_EXTENSIONS:
        return {
            "status": "error",
            "name": orig_name,
            "error": f"Unsupported file type. Supported types: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}",
        }
    if not _upload_title_from_filename(orig_name):
        return {
            "status": "error",
            "name": orig_name,
            "error": "Filename must include a meaningful title before its extension.",
        }
    if size > 31457280:
        return {"status": "error", "name": orig_name, "error": f"'{orig_name}' exceeds maximum 30MB constraint."}
    return None


def _find_existing_doc_by_hash(file_hash: str, user_id: str | None = None) -> dict[str, Any] | None:
    """Return only a same-tenant duplicate; private documents are never globally shared."""
    if not user_id:
        return None
    return surreal_db.get_document_by_hash(file_hash, str(user_id))


def _get_dedup_field(existing_doc: Any, field_name: str, default: Any = None) -> Any:
    """Helper to extract field values from either an ORM model or dict."""
    if hasattr(existing_doc, field_name):
        return getattr(existing_doc, field_name)
    if isinstance(existing_doc, dict):
        return existing_doc.get(field_name, default)
    return default


def _clone_deduplicated_doc(request, existing_doc, orig_name: str, file_hash: str) -> dict[str, str]:
    import uuid

    ip = get_client_ip(request)
    new_uuid = str(uuid.uuid4())
    file_path = _get_dedup_field(existing_doc, "file")

    data = {
        "doc_uuid": new_uuid,
        "file": file_path,
        "original_filename": orig_name,
        "file_hash": file_hash,
        "status": "COMPLETED",
        "uploaded_by_id": view_common.get_request_actor_id(request),
        "language": _get_dedup_field(existing_doc, "language"),
        "author": _get_dedup_field(existing_doc, "author"),
        "title": _get_dedup_field(existing_doc, "title"),
        "publisher": _get_dedup_field(existing_doc, "publisher"),
        "publication_year": _get_dedup_field(existing_doc, "publication_year"),
        "license_type": _get_dedup_field(existing_doc, "license_type"),
        "doi": _get_dedup_field(existing_doc, "doi"),
        "document_type": _get_dedup_field(existing_doc, "document_type"),
        "page_count": _get_dedup_field(existing_doc, "page_count", 0),
        "raw_markdown": _get_dedup_field(existing_doc, "raw_markdown"),
        "refined_markdown": _get_dedup_field(existing_doc, "refined_markdown"),
        "yaml_metadata": _get_dedup_field(existing_doc, "yaml_metadata"),
        "qa_dataset": _get_dedup_field(existing_doc, "qa_dataset"),
        "cost_usd": 0.0,
        "input_tokens": 0,
        "output_tokens": 0,
        "semantic_signature": _get_dedup_field(existing_doc, "semantic_signature"),
        "retry_count": 0,
        "created_at": view_common.format_datetime(timezone.now()),
        "updated_at": view_common.format_datetime(timezone.now()),
        "expires_at": view_common.format_datetime(
            timezone.now() + timezone.timedelta(days=int(getattr(settings, "DATA_RETENTION_DAYS", 30)))
        ),
    }
    doc = surreal_db.create_document(data)

    try:
        source_doc_uuid = view_common._extract_clean_doc_uuid(existing_doc)
        if source_doc_uuid:
            surreal_db.clone_chunks(source_doc_uuid, new_uuid)
    except Exception as clone_err:
        # The clone is marked COMPLETED but has no searchable chunks; enqueue a
        # re-embed so the copied markdown is re-chunked and embedded instead of
        # leaving a silently RAG-invisible document.
        logger.warning("[Upload] SurrealDB chunk clone failed for %s: %s", new_uuid, clone_err)
        try:
            from extractor import cloud_tasks
            from extractor.utils import REEMBED_DOCUMENT_TASK

            if getattr(settings, "SURREALDB_OFFLINE", False):
                doc_id = doc.get("id") if isinstance(doc, dict) else getattr(doc, "id", None)
                cloud_tasks.enqueue(REEMBED_DOCUMENT_TASK, {"document_id": doc_id})
            else:
                cloud_tasks.enqueue(REEMBED_DOCUMENT_TASK, {"document_uuid": new_uuid})
        except Exception as enqueue_err:
            logger.warning("[Upload] Failed to enqueue chunk-repair re-embed for %s: %s", new_uuid, enqueue_err)

    view_common._record_view_audit(
        request,
        AuditAction.UPLOAD_CACHED,
        f"File '{orig_name}' uploaded and instantly cached via de-duplication.",
        document=doc,
        ip=ip,
    )
    # The clone belongs to the acting user, not the source document's owner.
    broadcast_status_change(new_uuid, "COMPLETED", owner_id=view_common.get_request_actor_id(request))
    return {"status": "cached", "name": orig_name}


def _retry_failed_doc(request, existing_doc, orig_name: str) -> dict[str, str]:
    from extractor import cloud_tasks

    ip = get_client_ip(request)
    retry_cnt = existing_doc.retry_count if hasattr(existing_doc, "retry_count") else existing_doc.get("retry_count", 0)
    if retry_cnt >= 3:
        return {
            "status": "error",
            "name": orig_name,
            "error": f"Maximum retry limit of 3 exceeded for file '{orig_name}'.",
        }

    doc_uuid = view_common._extract_clean_doc_uuid(existing_doc)
    actor_id = view_common.get_request_actor_id(request)
    # CAS on FAILED: a concurrent retry/reaper/cancel between the dedup read and
    # this write must not be clobbered back to PENDING. Spend fields are left
    # untouched — zeroing them would erase already-incurred cost from budget
    # accounting (see AUD-311).
    doc_ref = surreal_db.update_document_if_status(
        doc_uuid,
        {
            "status": "PENDING",
            PENDING_OPERATION: "",
            "error_message": "",
            "cancel_requested": False,
            "retry_count": retry_cnt + 1,
        },
        allowed_statuses=["FAILED"],
    )
    if doc_ref is None:
        return {
            "status": "error",
            "name": orig_name,
            "error": f"File '{orig_name}' changed state while retrying; refresh and try again.",
        }

    view_common._record_view_audit(
        request,
        AuditAction.UPLOAD,
        f"File '{orig_name}' re-uploaded; resetting failed pipeline status and re-enqueuing.",
        document=doc_ref,
        ip=ip,
    )

    if getattr(settings, "SURREALDB_OFFLINE", False):
        doc_id = doc_ref.get("id") if isinstance(doc_ref, dict) else getattr(doc_ref, "id", None)
        cloud_tasks.enqueue("process_document", {"document_id": doc_id})
    else:
        cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})

    # Reprocessing may change derived content; drop cached answers built on it.
    view_common._invalidate_doc_rag_caches(doc_uuid, actor_id)
    broadcast_status_change(str(doc_uuid), "PENDING", owner_id=actor_id)
    return {"status": "success", "name": f"{orig_name} (re-enqueued)"}


def _create_fresh_uploaded_doc(request, uploaded_file, file_hash: str) -> dict[str, str]:
    import os
    import uuid

    from django.core.files.storage import default_storage

    from extractor import cloud_tasks

    orig_name = uploaded_file.name
    ip = get_client_ip(request)
    title_guess = _upload_title_from_filename(orig_name)
    ext_guess = os.path.splitext(orig_name)[1].replace(".", "").upper() or "PDF"
    if ext_guess in {ext.lstrip(".").upper() for ext in VIDEO_FILE_EXTENSIONS}:
        ext_guess = "VIDEO"

    file_id = str(uuid.uuid4())
    ext = os.path.splitext(orig_name)[1].lower()
    filename = f"uploads/{timezone.now().strftime('%Y/%m/%d')}/{file_id}{ext}"
    saved_path = default_storage.save(filename, uploaded_file)

    new_uuid = str(uuid.uuid4())
    data = {
        "doc_uuid": new_uuid,
        "file": saved_path,
        "original_filename": orig_name,
        "file_hash": file_hash,
        "status": "PENDING",
        "uploaded_by_id": view_common.get_request_actor_id(request),
        "title": title_guess,
        "document_type": ext_guess,
        "retry_count": 0,
        "created_at": view_common.format_datetime(timezone.now()),
        "updated_at": view_common.format_datetime(timezone.now()),
    }
    doc_created = False
    try:
        doc = surreal_db.create_document(data)
        doc_created = True

        view_common._record_view_audit(
            request,
            AuditAction.UPLOAD,
            f"File '{orig_name}' uploaded successfully (size: {uploaded_file.size} bytes).",
            document=doc,
            ip=ip,
        )

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc.get("id") if isinstance(doc, dict) else getattr(doc, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": new_uuid})
    except Exception:
        if doc_created:
            # Remove the Surreal record too — otherwise the reaper picks up a
            # PENDING doc whose backing file was just deleted and burns a
            # retry cycle failing on a missing blob.
            try:
                surreal_db.delete_document(new_uuid)
            except Exception as del_exc:
                logger.warning("[Upload] Failed to delete orphaned document %s: %s", new_uuid, del_exc)
        try:
            default_storage.delete(saved_path)
        except Exception as cleanup_exc:
            logger.warning("[Upload] Failed to clean up orphaned file %s: %s", saved_path, cleanup_exc)
        raise
    return {"status": "success", "name": orig_name}


def _handle_existing_doc_match(
    request, existing_doc, orig_name: str, file_hash: str, actor_id: str
) -> dict[str, str] | None:
    status = existing_doc.get("status")
    uploaded_by_id = existing_doc.get("uploaded_by_id")
    if status == "COMPLETED":
        if uploaded_by_id == actor_id:
            logger.info(
                "[Deduplication] User already has completed document with hash %s. Reusing without copy.",
                file_hash,
            )
            return {"status": "cached", "name": orig_name}

        return _clone_deduplicated_doc(request, existing_doc, orig_name, file_hash)
    elif status in ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]:
        return {
            "status": "error",
            "name": orig_name,
            "error": f"File '{orig_name}' is already being processed by the background worker.",
        }
    elif status == "FAILED":
        return _retry_failed_doc(request, existing_doc, orig_name)
    return None


def _process_single_upload_file(request, uploaded_file, processed_hashes: set) -> dict[str, str]:
    orig_name = uploaded_file.name
    validation_error = _validate_upload_file(orig_name, uploaded_file.size)
    if validation_error:
        return validation_error

    file_hash = calculate_file_sha256(uploaded_file)
    if file_hash in processed_hashes:
        return {
            "status": "error",
            "name": orig_name,
            "error": f"File '{orig_name}' is a duplicate of another file in this batch.",
        }

    processed_hashes.add(file_hash)
    actor_id = view_common.get_request_actor_id(request)
    existing_doc = _find_existing_doc_by_hash(file_hash, actor_id)

    try:
        if existing_doc:
            res = _handle_existing_doc_match(request, existing_doc, orig_name, file_hash, actor_id)
            if res is not None:
                return res

        return _create_fresh_uploaded_doc(request, uploaded_file, file_hash)
    except Exception as e:
        return {"status": "error", "name": orig_name, "error": f"Error processing '{orig_name}': {e!s}"}


class UploadView(LoginRequiredMixin, View):
    """
    Handles secure drag-and-drop document uploads with automatic SHA-256
    content de-duplication to guarantee zero wasted storage on identical uploads.
    Supports multi-file batch uploads.
    """

    def _clone_deduplicated_document(self, request, existing_doc, orig_name, file_hash):
        return _clone_deduplicated_doc(request, existing_doc, orig_name, file_hash)

    def _retry_existing_failed_document(self, request, existing_doc, orig_name):
        return _retry_failed_doc(request, existing_doc, orig_name)

    def _create_fresh_document(self, request, uploaded_file, file_hash):
        return _create_fresh_uploaded_doc(request, uploaded_file, file_hash)

    def _validate_uploaded_file(self, orig_name, size):
        return _validate_upload_file(orig_name, size)

    def _find_existing_doc(self, file_hash, user_id):
        return _find_existing_doc_by_hash(file_hash, user_id)

    def _handle_existing_doc_match(self, request, existing_doc, orig_name, file_hash, actor_id):
        return _handle_existing_doc_match(request, existing_doc, orig_name, file_hash, actor_id)

    def _process_single_file(self, request, uploaded_file, processed_hashes):
        return _process_single_upload_file(request, uploaded_file, processed_hashes)

    def post(self, request):
        uploaded_files = request.FILES.getlist("file")
        if not uploaded_files:
            messages.error(request, "No file uploaded.")
            return redirect("dashboard")
        if len(uploaded_files) > MAX_UPLOAD_BATCH_SIZE:
            messages.error(request, f"Too many files in one batch (max {MAX_UPLOAD_BATCH_SIZE}).")
            return redirect("dashboard")

        # Per-user throttle: batches can contain up to DATA_UPLOAD_MAX_NUMBER_FILES
        # files, so unbounded upload POSTs could flood storage and the task queue.
        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"upload:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=10, window_seconds=60):
            messages.error(request, view_common.ERR_RATE_LIMIT)
            return redirect("dashboard")

        # Budget pre-check: block new LLM processing when monthly budget is exhausted
        if view_common._is_budget_exceeded(request.user, actor_id):
            messages.error(
                request,
                "Monthly AI budget has been reached. New document processing is paused until the budget resets "
                "or an administrator increases the budget cap.",
            )
            return redirect("dashboard")

        successful_uploads = []
        cached_uploads = []
        errors = []
        processed_hashes = set()

        for uploaded_file in uploaded_files:
            res = self._process_single_file(request, uploaded_file, processed_hashes)
            if res["status"] == "success":
                successful_uploads.append(res["name"])
            elif res["status"] == "cached":
                cached_uploads.append(res["name"])
            else:
                errors.append(res["error"])

        if successful_uploads:
            messages.success(request, f"Successfully uploaded and queued: {', '.join(successful_uploads)}.")
        if cached_uploads:
            messages.success(
                request, f"Instantly cached from deduplication ($0 USD cost saved): {', '.join(cached_uploads)}."
            )
        if errors:
            messages.error(request, f"Some uploads failed: {'; '.join(errors)}")

        return redirect("dashboard")
