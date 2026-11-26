import logging
from typing import Any

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from extractor import surreal_db, view_common
from extractor.models import AuditAction
from extractor.task_state import CANCEL_REQUESTED
from extractor.utils import (
    APPLICATION_JSON,
    HEADER_X_REQUESTED_WITH,
    REEMBED_DOCUMENT_TASK,
    VALUE_XML_HTTP_REQUEST,
    broadcast_status_change,
    format_localized_cost,
    get_client_ip,
    get_locale_currency_details,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

CANCEL_PERM_DENIED_MSG = "Permission denied to cancel this document."
DOCUMENT_DELETE_FAILURE_MSG = "Document deletion could not be completed. Please try again later."
DOCUMENT_CANCEL_FAILURE_MSG = "Document processing could not be stopped. Please try again later."


@method_decorator(never_cache, name="dispatch")
class DocumentDetailView(LoginRequiredMixin, View):
    """
    Renders details for a single document, incorporating a dual-screen Markdown Split-Editor
    and interactive Q&A dataset panels.
    """

    def get(self, request, doc_uuid):
        if not doc_uuid or len(str(doc_uuid)) > 128:
            from django.http import Http404

            raise Http404(view_common.DOCUMENT_NOT_FOUND_MSG)

        try:
            raw_doc, doc, is_authorized, _ = view_common._get_authorized_wrapped_doc(
                request, doc_uuid, allow_unowned=True
            )
        except RuntimeError:
            logger.warning("[Detail View] Knowledge store unreachable for doc %s; rendering 503 page.", doc_uuid)
            return render(request, "extractor/service_unavailable.html", status=503)
        if not raw_doc:
            from django.http import Http404

            raise Http404(view_common.DOCUMENT_NOT_FOUND_MSG)

        if not is_authorized:
            messages.error(request, "Permission denied to view this document.")
            return redirect("dashboard")

        currency_details = get_locale_currency_details(request)

        # Render markdown to HTML for presentation (sanitized via Bleach inside render_markdown_to_html)
        rendered_raw = view_common._render_sanitized_markdown(doc.raw_markdown) if doc.raw_markdown else ""
        rendered_refined = view_common._render_sanitized_markdown(doc.refined_markdown) if doc.refined_markdown else ""

        # Live cost localization formatting
        formatted_cost = format_localized_cost(doc.cost_usd, currency_details)

        parsed_yaml = {}
        if doc.yaml_metadata:
            try:
                import yaml  # type: ignore[import-untyped]

                from extractor.tasks import _sanitise_yaml_block

                try:
                    meta_raw = yaml.safe_load(doc.yaml_metadata)
                except yaml.YAMLError:
                    meta_raw = yaml.safe_load(_sanitise_yaml_block(doc.yaml_metadata))
                if isinstance(meta_raw, dict):
                    parsed_yaml = {str(k).strip().lower(): v for k, v in meta_raw.items()}
                    parsed_yaml["source_link"] = view_common._safe_metadata_source_link(parsed_yaml.get("source_link"))
            except Exception as e:
                logger.debug("[Detail View] Failed to parse document YAML metadata: %s", e)

        context = {
            "document": doc,
            "rendered_raw": rendered_raw,
            "rendered_refined": rendered_refined,
            "formatted_cost": formatted_cost,
            "currency_details": currency_details,
            "parsed_yaml": parsed_yaml,
        }
        return render(request, "extractor/document_detail.html", context)


def _build_document_save_payload(request, sanitized_markdown: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "refined_markdown": sanitized_markdown,
        "status": "PENDING",
        CANCEL_REQUESTED: False,
    }
    field_limits = {
        "title": 255,
        "author": 255,
        "language": 50,
        "publisher": 255,
        "publication_year": 20,
        "license_type": 100,
        "doi": 100,
    }
    for field, max_len in field_limits.items():
        val = request.POST.get(field)
        if val is not None:
            payload[field] = val.strip()[:max_len]
    return payload


class DocumentSaveView(LoginRequiredMixin, View):
    """
    Saves edited Markdown text straight from the split pane editor
    and refreshes the physical file back to storage.
    """

    def post(self, request, doc_uuid):
        if not doc_uuid or len(str(doc_uuid)) > 128:
            messages.error(request, view_common.DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        raw_doc, _doc, is_authorized, users_map = view_common._get_authorized_wrapped_doc(request, doc_uuid)
        if not raw_doc:
            messages.error(request, view_common.DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        if not is_authorized:
            messages.error(request, "Permission denied to modify this document.")
            return redirect("dashboard")

        doc_status = raw_doc.get("status") if isinstance(raw_doc, dict) else getattr(raw_doc, "status", "")
        if doc_status in ("PENDING", "EXTRACTING", "REFINING", "EMBEDDING"):
            messages.error(
                request,
                "Document is currently being processed. Stop processing or wait for completion before editing.",
            )
            return redirect(view_common.ROUTE_DOCUMENT_DETAIL, doc_uuid=doc_uuid)

        new_markdown = request.POST.get("refined_markdown", "")
        if len(new_markdown) > 5 * 1024 * 1024:
            messages.error(request, "Edited markdown exceeds maximum 5MB size limit.")
            return redirect(view_common.ROUTE_DOCUMENT_DETAIL, doc_uuid=doc_uuid)

        # Every save enqueues a re-embed Cloud Task; bound save frequency so
        # repeated edits cannot flood the queue with embedding spend.
        save_rl_key = f"doc_save:{view_common.get_request_actor_id(request)}"
        if not view_common._enforce_distributed_rate_limit(
            save_rl_key, save_rl_key, max_requests=30, window_seconds=60
        ):
            messages.error(request, view_common.ERR_RATE_LIMIT)
            return redirect(view_common.ROUTE_DOCUMENT_DETAIL, doc_uuid=doc_uuid)

        # Strip control null bytes and normalize unicode without destroying raw Markdown link/comparison syntax
        cleaned_markdown = new_markdown.replace("\x00", "").strip()
        payload = _build_document_save_payload(request, cleaned_markdown)

        doc_ref = surreal_db.update_document(doc_uuid, payload)
        doc_wrapped = view_common._wrap_surreal_doc(doc_ref, users_map)

        # Invalidate cached RAG answers derived from the pre-edit content.
        view_common._invalidate_doc_rag_caches(str(doc_uuid), view_common.get_request_actor_id(request))

        view_common._record_view_audit(
            request,
            AuditAction.DOCUMENT_EDITED,
            f"Document '{doc_wrapped.original_filename}' content modified and re-embedding queued.",
            document=doc_wrapped,
        )

        # Re-embed after edit to keep SurrealDB HNSW memory in sync with editor changes
        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc_wrapped.get("id") if isinstance(doc_wrapped, dict) else getattr(doc_wrapped, "id", None)
            cloud_tasks.enqueue(REEMBED_DOCUMENT_TASK, {"document_id": doc_id})
        else:
            cloud_tasks.enqueue(REEMBED_DOCUMENT_TASK, {"document_uuid": doc_uuid})
        broadcast_status_change(str(doc_uuid), "PENDING", owner_id=view_common._doc_owner_id(doc_wrapped))

        messages.success(request, "Changes saved and re-indexing queued successfully!")
        return redirect(view_common.ROUTE_DOCUMENT_DETAIL, doc_uuid=doc_uuid)


class DocumentDeleteView(LoginRequiredMixin, View):
    """
    Reference-counted document deletion.
    If multiple DB entries point to the same content address (SHA-256),
    the GCS file is preserved. Otherwise, it is deleted cleanly.
    """

    def _count_shared_references(self, doc_uuid, file_hash):
        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            from extractor.models import SourceDocument

            try:
                int_id = int(doc_uuid)
                return (
                    SourceDocument.objects.filter(file_hash=file_hash)
                    .exclude(Q(id=int_id) | Q(uuid=str(doc_uuid)))
                    .count()
                )
            except ValueError, TypeError:
                return SourceDocument.objects.filter(file_hash=file_hash).exclude(uuid=str(doc_uuid)).count()

        sql = "SELECT doc_uuid FROM documents WHERE file_hash = $file_hash;"
        rows = surreal_db._first_result(surreal_db._run(sql, {"file_hash": file_hash}))
        other_uuids = [r["doc_uuid"] for r in rows if str(r.get("doc_uuid")) != str(doc_uuid)]
        return len(other_uuids)

    @staticmethod
    def _purge_physical_file(file_rel_path: str, file_hash: str, shared_references: int) -> None:
        if shared_references != 0:
            logger.info(
                "[De-duplication Delete] Preserved file hash %s (Shared with %s entries).", file_hash, shared_references
            )
            return
        try:
            from django.core.files.storage import default_storage

            path_str = str(file_rel_path or "").strip()
            if path_str and default_storage.exists(path_str):
                default_storage.delete(path_str)
            logger.info("[De-duplication Delete] Purged file hash %s physically.", file_hash)
        except Exception as e:
            logger.warning("[De-duplication Delete] Failed to physically delete file for hash %s: %s", file_hash, e)
            raise RuntimeError("Stored file could not be deleted. The document was retained; retry deletion.") from e

    @staticmethod
    def _abort_active_processing(doc, doc_uuid) -> None:
        if getattr(doc, "status", None) not in ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]:
            return
        from extractor.cloud_tasks import cancel_document_task

        cancel_document_task(doc_uuid, surreal_db.get_document(doc_uuid) or {})
        broadcast_status_change(str(doc_uuid), "FAILED", owner_id=view_common._doc_owner_id(doc))

    def post(self, request, doc_uuid):
        is_ajax = (
            request.headers.get(HEADER_X_REQUESTED_WITH) == VALUE_XML_HTTP_REQUEST
            or request.headers.get("accept") == APPLICATION_JSON
        )
        raw_doc, doc, is_authorized, _ = view_common._get_authorized_wrapped_doc(request, doc_uuid)
        if not raw_doc:
            if is_ajax:
                return JsonResponse({"error": view_common.DOCUMENT_NOT_FOUND_MSG}, status=404)
            messages.error(request, view_common.DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        if not is_authorized:
            if is_ajax:
                return JsonResponse({"error": "Permission denied to delete this document."}, status=403)
            messages.error(request, "Permission denied to delete this document.")
            return redirect("dashboard")

        file_hash = doc.file_hash
        orig_name = doc.original_filename
        shared_references = self._count_shared_references(doc_uuid, file_hash)

        try:
            self._abort_active_processing(doc, doc_uuid)
            self._purge_physical_file(raw_doc.get("file", ""), file_hash, shared_references)
        except RuntimeError:
            logger.exception("Document deletion failed for %s", doc_uuid)
            if is_ajax:
                return JsonResponse({"error": DOCUMENT_DELETE_FAILURE_MSG}, status=503)
            messages.error(request, DOCUMENT_DELETE_FAILURE_MSG)
            return redirect("dashboard")
        surreal_db.delete_document(doc_uuid)

        view_common._record_view_audit(
            request,
            AuditAction.DELETE,
            f"Deleted document '{orig_name}' (Title: {doc.title}). Shared references remaining: {shared_references}.",
            ip=get_client_ip(request),
        )

        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            from extractor.models import SourceDocument

            SourceDocument.objects.filter(uuid=doc_uuid).delete()
        else:
            try:
                from extractor.models import SourceDocument

                SourceDocument.objects.filter(uuid=doc_uuid).delete()
            except Exception as sqlite_cleanup_err:
                logger.debug("[Delete] SQLite cleanup for doc_uuid=%s skipped: %s", doc_uuid, sqlite_cleanup_err)

        broadcast_status_change(str(doc_uuid), "DELETED", owner_id=raw_doc.get("uploaded_by_id"))

        if is_ajax:
            return JsonResponse({"status": "success", "message": f"Document '{orig_name}' deleted successfully."})

        messages.success(request, f"Document '{orig_name}' deleted successfully.")
        return redirect("dashboard")


class DocumentPurgeAllView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Enables instant GDPR 'Right to be Forgotten' data minimization.
    Deletes all records and physically flushes matching files cleanly.
    """

    def test_func(self):
        user = getattr(self.request, "user", None)
        return bool(user and getattr(user, "is_superuser", False))

    @staticmethod
    def _delete_physical_files(raw_docs):
        """Delete the physical file on storage for each document record."""
        from django.core.files.storage import default_storage

        for doc in raw_docs:
            file_path = doc.get("file")
            if file_path:
                try:
                    if default_storage.exists(file_path):
                        default_storage.delete(file_path)
                except Exception as e:
                    logger.warning("[Purge All] Failed to delete file for %s: %s", doc.get("title"), e)
                    raise RuntimeError("Storage cleanup failed. Document records were retained for retry.") from e

    @staticmethod
    def _abort_inflight_docs(raw_docs):
        """Broadcast FAILED to any in-flight workers before wiping SurrealDB."""
        _IN_FLIGHT = {"PENDING", "EXTRACTING", "REFINING", "EMBEDDING"}
        for doc in raw_docs:
            if doc.get("status", "") not in _IN_FLIGHT:
                continue
            doc_uuid = doc.get("doc_uuid") or doc.get("id")
            if not doc_uuid:
                continue
            from extractor.cloud_tasks import cancel_document_task

            cancel_document_task(doc_uuid, surreal_db.get_document(doc_uuid) or doc)
            broadcast_status_change(str(doc_uuid), "FAILED", owner_id=doc.get("uploaded_by_id"))

    def post(self, request):
        # The RESET confirmation is enforced server-side; the template gate is
        # only a UX affordance and cannot be trusted to protect a global wipe.
        if request.POST.get("confirm") != "RESET":
            messages.error(request, "Type RESET in the confirmation field to permanently wipe all data.")
            return redirect("dashboard")

        raw_docs = surreal_db.list_documents()
        ip = get_client_ip(request)

        try:
            self._abort_inflight_docs(raw_docs)
            self._delete_physical_files(raw_docs)
            surreal_db.purge_all()
            try:
                from extractor.models import MonthlySpendLog, SourceDocument, UserMemory

                SourceDocument.objects.all().delete()
                UserMemory.objects.all().delete()
                if getattr(settings, "SURREALDB_OFFLINE", False):
                    MonthlySpendLog.objects.all().delete()
            except Exception as sqlite_err:
                raise RuntimeError("Local record cleanup failed; purge requires retry.") from sqlite_err
        except Exception as exc:
            logger.exception("[Purge All] Reset could not be completed: %s", exc)
            messages.error(
                request,
                "Reset incomplete. Some files may already be removed; retry after resolving the storage or database error.",
            )
            return redirect("dashboard")

        view_common._record_view_audit(
            request,
            AuditAction.PURGE_ALL,
            f"Purged all documents and associated semantic memory vector embeddings. Count: {len(raw_docs)}.",
            ip=ip,
        )

        try:
            from django.core.files.storage import default_storage

            _dirs, files = default_storage.listdir("chunks")
            for f in files:
                default_storage.delete(f"chunks/{f}")
            logger.info("[Purge All] Cleaned all chunk JSON files from storage.")
        except Exception as storage_err:
            logger.warning("[Purge All] Failed to clean chunks folder: %s", storage_err)
            messages.warning(
                request,
                "Document reset completed, but backup chunk cleanup failed. Retry cleanup before claiming full erasure.",
            )
            return redirect("dashboard")

        messages.success(request, "Reset Memory Complete: Purged all documents and vector embeddings.")
        return redirect("dashboard")


def _is_doc_owner_or_staff(doc: dict, request: Any) -> bool:
    if request.user.is_staff or request.user.is_superuser:
        return True
    uploaded_by_id = str(doc.get("uploaded_by_id") or "")
    if not uploaded_by_id:
        return False
    actor_id = view_common.get_request_actor_id(request)
    return uploaded_by_id in (actor_id, str(getattr(request.user, "id", "")))


def _enqueue_restarted_document(doc: dict, doc_uuid: str, cloud_tasks: Any) -> None:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        doc_id = doc.get("id") if isinstance(doc, dict) else getattr(doc, "id", None)
        cloud_tasks.enqueue("process_document", {"document_id": doc_id})
    else:
        cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})
    broadcast_status_change(doc_uuid, "PENDING", owner_id=doc.get("uploaded_by_id"))


def _restart_single_document(doc, request, cloud_tasks):
    doc_uuid = view_common._extract_clean_doc_uuid(doc)
    if not doc_uuid or not _is_doc_owner_or_staff(doc, request):
        return False

    status = doc.get("status")
    retry_cnt = int(doc.get("retry_count") or 0)
    if status == "FAILED" and retry_cnt >= 3:
        return False
    if status not in ["PENDING", "FAILED", "COMPLETED"]:
        return False

    surreal_db.update_document(
        doc_uuid,
        {
            "status": "PENDING",
            "cost_usd": 0.0,
            "input_tokens": 0,
            "output_tokens": 0,
            "retry_count": retry_cnt + 1 if status == "FAILED" else retry_cnt,
            "error_message": "",
            CANCEL_REQUESTED: False,
            "updated_at": view_common.format_datetime(timezone.now()),
        },
    )

    _enqueue_restarted_document(doc, doc_uuid, cloud_tasks)
    # Reprocessing may change refined content; drop cached answers derived
    # from the previous pipeline output for this document's owner.
    view_common._invalidate_doc_rag_caches(str(doc_uuid), doc.get("uploaded_by_id"))
    doc_title = doc.get("title") or doc.get("original_filename") or doc_uuid
    view_common._record_view_audit(
        request,
        AuditAction.DOCUMENT_REQUEUED,
        f"User re-enqueued pipeline processing for document '{doc_title}'.",
    )
    return True


def _handle_bulk_restart(request, document_ids):
    from django.conf import settings

    from extractor import cloud_tasks

    restarted_count = 0
    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.surreal_db import _model_to_dict

        docs = [_model_to_dict(d) for d in _get_offline_docs_for_delete(request, document_ids)]
    else:
        clean_ids = [view_common._extract_clean_doc_uuid(i) for i in document_ids if i]
        docs = surreal_db.get_documents(clean_ids)

    for doc in docs:
        if _restart_single_document(doc, request, cloud_tasks):
            restarted_count += 1
    messages.success(request, f"Successfully queued {restarted_count} tasks for reprocessing.")


def _cleanup_file_storage(file_rel_path, shared_references: int, default_storage) -> None:
    if shared_references == 0:
        try:
            path_str = str(file_rel_path or "").strip()
            if path_str and default_storage.exists(path_str):
                default_storage.delete(path_str)
        except Exception as e:
            logger.warning("[Bulk Delete] Failed to physically delete file: %s", e)
            raise RuntimeError("Stored file could not be deleted. The document was retained; retry deletion.") from e


def _abort_inflight_processing(doc, doc_uuid, surreal_db) -> None:
    if getattr(doc, "status", None) in ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]:
        try:
            from extractor.cloud_tasks import cancel_document_task

            raw_surreal_doc = surreal_db.get_document(doc_uuid) if hasattr(surreal_db, "get_document") else {}
            cancel_document_task(doc_uuid, raw_surreal_doc or {})
            broadcast_status_change(str(doc_uuid), "FAILED", owner_id=view_common._doc_owner_id(doc))
        except Exception as abort_err:
            logger.debug("[Bulk Delete] Could not cancel in-flight doc %s: %s", doc_uuid, abort_err)


def _delete_db_record(doc, doc_uuid, surreal_db) -> None:
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        if isinstance(doc, SourceDocument):
            doc.delete()
        else:
            surreal_db.delete_document(doc_uuid)
    else:
        surreal_db.delete_document(doc_uuid)
        try:
            from extractor.models import SourceDocument

            SourceDocument.objects.filter(uuid=doc_uuid).delete()
        except Exception as sqlite_cleanup_err:
            logger.debug("[Delete] SQLite cleanup for doc_uuid=%s skipped: %s", doc_uuid, sqlite_cleanup_err)


def _delete_single_document(doc, hash_ref_counts, request, default_storage, surreal_db):
    file_hash = getattr(doc, "file_hash", None)
    orig_name = getattr(doc, "original_filename", "Unknown")
    doc_uuid = view_common._extract_clean_doc_uuid(doc)

    total_refs = hash_ref_counts.get(file_hash, 0)
    shared_references = max(0, total_refs - 1)

    _abort_inflight_processing(doc, doc_uuid, surreal_db)
    _cleanup_file_storage(getattr(doc, "file", None), shared_references, default_storage)

    if file_hash in hash_ref_counts:
        hash_ref_counts[file_hash] = shared_references

    _delete_db_record(doc, doc_uuid, surreal_db)

    view_common._record_view_audit(
        request,
        AuditAction.DELETE,
        f"Bulk Deleted document '{orig_name}' (Title: {getattr(doc, 'title', orig_name)}). Shared references remaining: {shared_references}.",
    )

    if doc_uuid:
        broadcast_status_change(str(doc_uuid), "DELETED", owner_id=view_common._doc_owner_id(doc))


def _get_offline_docs_for_delete(request, document_ids):

    from extractor.models import SourceDocument

    int_ids = []
    uuid_strs = []
    for item in document_ids:
        try:
            int_ids.append(int(item))
        except ValueError, TypeError:
            clean_str = view_common._extract_clean_doc_uuid(item)
            if clean_str:
                uuid_strs.append(clean_str)

    qs = SourceDocument.objects.filter(Q(id__in=int_ids) | Q(uuid__in=uuid_strs))
    if not (request.user.is_staff or request.user.is_superuser):
        qs = qs.filter(uploaded_by=request.user)
    return list(qs)


def _get_surreal_docs_for_delete(request, document_ids, users_map=None):
    clean_ids = [view_common._extract_clean_doc_uuid(i) for i in document_ids if i]
    raw_docs = surreal_db.get_documents(clean_ids)
    if users_map is None:
        user_ids = {d.get("uploaded_by_id") for d in raw_docs if d and d.get("uploaded_by_id")}
        users_map = view_common._build_users_map(user_ids, fallback_user=request.user)
    docs = []
    actor_id = view_common.get_request_actor_id(request)
    for raw_doc in raw_docs:
        uploaded_by_id = str(raw_doc.get("uploaded_by_id") or "")
        is_authorized = (
            request.user.is_staff
            or request.user.is_superuser
            or (
                uploaded_by_id
                and (
                    uploaded_by_id == actor_id
                    or (hasattr(request.user, "id") and uploaded_by_id == str(request.user.id))
                )
            )
        )
        if not is_authorized:
            continue
        docs.append(view_common._wrap_surreal_doc(raw_doc, users_map))
    return docs


def _get_docs_for_delete(request, document_ids, users_map=None):
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _get_offline_docs_for_delete(request, document_ids)
    return _get_surreal_docs_for_delete(request, document_ids, users_map=users_map)


def _get_hash_ref_counts(file_hashes):
    from django.conf import settings

    hash_ref_counts = {}
    if not file_hashes:
        return hash_ref_counts

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from django.db.models import Count

        from extractor.models import SourceDocument

        counts = (
            SourceDocument.objects.filter(file_hash__in=file_hashes).values("file_hash").annotate(n=Count("file_hash"))
        )
        hash_ref_counts = {item["file_hash"]: item["n"] for item in counts}
    else:
        count_sql = "SELECT file_hash, count() AS n FROM documents WHERE file_hash IN $file_hashes GROUP BY file_hash;"
        res = surreal_db._first_result(surreal_db._run(count_sql, {"file_hashes": list(file_hashes)}))
        res = res or []
        hash_ref_counts = {r["file_hash"]: r.get("n", 0) for r in res if isinstance(r, dict) and "file_hash" in r}

    for file_hash in file_hashes:
        if file_hash not in hash_ref_counts:
            hash_ref_counts[file_hash] = 0

    return hash_ref_counts


def _handle_bulk_delete(request, document_ids):
    from django.core.files.storage import default_storage

    docs = _get_docs_for_delete(request, document_ids)
    file_hashes = {doc.file_hash for doc in docs if doc.file_hash}
    hash_ref_counts = _get_hash_ref_counts(file_hashes)

    deleted_count = 0
    failed_count = 0
    for doc in docs:
        try:
            _delete_single_document(doc, hash_ref_counts, request, default_storage, surreal_db)
            deleted_count += 1
        except Exception as exc:
            failed_count += 1
            logger.exception(
                "[Bulk Delete] Failed to delete document %s: %s", view_common._extract_clean_doc_uuid(doc), exc
            )

    if failed_count:
        messages.warning(request, f"Deleted {deleted_count} documents; {failed_count} could not be removed.")
    elif deleted_count:
        messages.success(request, f"Successfully deleted {deleted_count} documents from the repository.")
    else:
        messages.warning(request, "No selected documents could be deleted.")


class BulkDocumentActionView(LoginRequiredMixin, View):
    """
    Handles bulk operations (Delete or Reprocess/Restart) on selected documents.
    """

    def get(self, request):
        return redirect("dashboard")

    def post(self, request):
        action = request.POST.get("action")
        raw_ids = [d.strip() for d in request.POST.getlist("selected_documents") if d.strip()]
        document_ids = list(dict.fromkeys(raw_ids))[:100]
        if not document_ids:
            messages.error(request, "No documents selected.")
            return redirect("dashboard")
        if action == "restart":
            actor_id = view_common.get_request_actor_id(request)
            if view_common._is_budget_exceeded(request.user, actor_id):
                messages.error(request, "Monthly spend budget exceeded.")
                return redirect("dashboard")
            rl_key = f"bulk_restart:{actor_id}"
            if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=5, window_seconds=300):
                messages.error(request, view_common.ERR_RATE_LIMIT)
                return redirect("dashboard")
            _handle_bulk_restart(request, document_ids)
        elif action == "delete":
            _handle_bulk_delete(request, document_ids)
        else:
            messages.error(request, "Invalid bulk action requested.")
        return redirect("dashboard")


class DocumentRetryView(LoginRequiredMixin, View):
    """
    Resets the document status, costs, and token spent metrics,
    and re-queues the process_document_task background job.

    Response contract:
    - AJAX/JSON requests (detected via X-Requested-With: XMLHttpRequest or
      Accept: application/json headers) receive a JsonResponse.
    - Standard form POST requests receive a HTTP redirect to the dashboard view.
    """

    def _handle_retry_permissions_and_limits(self, request, doc):
        actor_id = view_common.get_request_actor_id(request)
        is_owner = (doc.uploaded_by is not None and doc.uploaded_by == request.user) or (
            getattr(doc, "uploaded_by_id", None) and doc.uploaded_by_id == actor_id
        )
        if not (request.user.is_staff or request.user.is_superuser or is_owner):
            return "Permission denied to retry this document.", 403

        # Refuse to re-queue a document that is actively in mid-flight (EXTRACTING, REFINING, EMBEDDING)
        # to prevent two concurrent workers on the same document from racing.
        _ACTIVE_PROCESSING = {"EXTRACTING", "REFINING", "EMBEDDING"}
        if doc.status in _ACTIVE_PROCESSING:
            return "Document is currently being processed. Stop it first before retrying.", 409

        is_restart = doc.status in ["COMPLETED", "PENDING"]
        retry_cnt = doc.retry_count

        if not is_restart and retry_cnt >= 3:
            return "Maximum retry limit of 3 exceeded for this document.", 400

        return None, None

    def _requeue_document_task(self, doc_wrapped, doc_uuid, request):
        view_common._record_view_audit(
            request,
            AuditAction.DOCUMENT_REQUEUED,
            f"User re-enqueued pipeline processing for document '{doc_wrapped.title or doc_wrapped.original_filename}'.",
            document=doc_wrapped,
        )

        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc_wrapped.get("id") if isinstance(doc_wrapped, dict) else getattr(doc_wrapped, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": str(doc_uuid)})

        # Reprocessing may produce different refined content; drop cached
        # answers derived from the previous pipeline output.
        view_common._invalidate_doc_rag_caches(
            str(doc_uuid),
            getattr(doc_wrapped, "uploaded_by_id", None) or view_common.get_request_actor_id(request),
        )

    @staticmethod
    def _requeue_spend_gate(request, is_ajax):
        """Budget + rate-limit gate for re-enqueueing the full pipeline."""
        actor_id = view_common.get_request_actor_id(request)
        if view_common._is_budget_exceeded(request.user, actor_id):
            if is_ajax:
                return JsonResponse({"error": "Monthly spend budget exceeded."}, status=402)
            messages.error(request, "Monthly spend budget exceeded.")
            return redirect("dashboard")
        rl_key = f"doc_retry:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=10, window_seconds=300):
            if is_ajax:
                return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)
            messages.error(request, view_common.ERR_RATE_LIMIT)
            return redirect("dashboard")
        return None

    def post(self, request, doc_uuid):
        raw_doc = surreal_db.get_document(doc_uuid)
        is_ajax = (
            request.headers.get(HEADER_X_REQUESTED_WITH) == VALUE_XML_HTTP_REQUEST
            or request.headers.get("accept") == APPLICATION_JSON
        )

        if not raw_doc:
            if is_ajax:
                return JsonResponse({"error": view_common.DOCUMENT_NOT_FOUND_MSG}, status=404)
            messages.error(request, view_common.DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        uid = raw_doc.get("uploaded_by_id")
        users_map = view_common._build_users_map([uid], fallback_user=request.user)
        doc = view_common._wrap_surreal_doc(raw_doc, users_map)

        err_msg, status_code = self._handle_retry_permissions_and_limits(request, doc)
        if err_msg:
            if is_ajax:
                return JsonResponse({"error": err_msg}, status=status_code)
            messages.error(request, err_msg)
            return redirect("dashboard")

        # Retry/restart of terminal-state documents is not bounded by
        # retry_count, so gate re-enqueue spend and task volume explicitly.
        gate_response = self._requeue_spend_gate(request, is_ajax)
        if gate_response is not None:
            return gate_response

        is_restart = doc.status in ["COMPLETED", "PENDING"]
        retry_cnt = doc.retry_count

        doc_ref = surreal_db.update_document(
            doc_uuid,
            {
                "status": "PENDING",
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "error_message": "",
                "retry_count": 0 if is_restart else retry_cnt + 1,
                CANCEL_REQUESTED: False,
            },
        )
        doc_wrapped = view_common._wrap_surreal_doc(doc_ref, users_map)

        self._requeue_document_task(doc_wrapped, doc_uuid, request)
        broadcast_status_change(str(doc_uuid), "PENDING", owner_id=view_common._doc_owner_id(doc_ref))

        if is_ajax:
            return JsonResponse({"status": "success", "message": "Curation pipeline re-enqueued."})

        messages.success(request, f"Re-enqueued curation pipeline for document: {doc.title or doc.original_filename}")
        return redirect("dashboard")


def _cancel_guard_response(request, raw_doc, doc, is_ajax, *, is_authorized: bool):
    """Return an early HttpResponse if the cancel preconditions are not met, else None."""
    if not raw_doc:
        if is_ajax:
            return JsonResponse({"error": view_common.DOCUMENT_NOT_FOUND_MSG}, status=404)
        messages.error(request, view_common.DOCUMENT_NOT_FOUND_MSG)
        return redirect("dashboard")

    if not is_authorized:
        if is_ajax:
            return JsonResponse({"error": CANCEL_PERM_DENIED_MSG}, status=403)
        messages.error(request, CANCEL_PERM_DENIED_MSG)
        return redirect("dashboard")

    if doc.status == "COMPLETED" or (doc.status == "FAILED" and not raw_doc.get(CANCEL_REQUESTED)):
        msg = f"Cannot cancel document in terminal state '{doc.status}'."
        if is_ajax:
            return JsonResponse({"error": msg}, status=400)
        messages.error(request, msg)
        return redirect("dashboard")

    return None


class DocumentCancelView(LoginRequiredMixin, View):
    """
    Cancels an in-flight or pending document processing task,
    marking its status as FAILED with an explicit cancellation message.
    """

    def post(self, request, doc_uuid):
        is_ajax = (
            request.headers.get(HEADER_X_REQUESTED_WITH) == VALUE_XML_HTTP_REQUEST
            or request.headers.get("accept") == APPLICATION_JSON
        )
        raw_doc, doc, is_authorized, _ = view_common._get_authorized_wrapped_doc(request, doc_uuid)
        guard = _cancel_guard_response(request, raw_doc, doc, is_ajax, is_authorized=is_authorized)
        if guard is not None:
            return guard

        from extractor.cloud_tasks import cancel_document_task

        try:
            cancel_document_task(doc_uuid, raw_doc)
        except RuntimeError:
            logger.exception("Document task cancellation failed for %s", doc_uuid)
            if is_ajax:
                return JsonResponse({"error": DOCUMENT_CANCEL_FAILURE_MSG}, status=503)
            messages.error(request, DOCUMENT_CANCEL_FAILURE_MSG)
            return redirect("dashboard")
        broadcast_status_change(str(doc_uuid), "FAILED", owner_id=raw_doc.get("uploaded_by_id"))

        view_common._record_view_audit(
            request,
            AuditAction.DOCUMENT_EDITED,
            f"User manually stopped curation for document '{doc.original_filename}'.",
            document=doc,
        )

        if is_ajax:
            return JsonResponse({"status": "success", "message": "Document processing stopped."})

        messages.info(request, f"Stopped processing for document: {doc.title or doc.original_filename}")
        return redirect("dashboard")
