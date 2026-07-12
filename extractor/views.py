import logging
from datetime import UTC, datetime
from decimal import Decimal

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views import View

logger = logging.getLogger(__name__)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    # Statically import ingest_sources to satisfy desloppify importer analyzer
    import extractor.management.commands.ingest_sources  # noqa: F401

from extractor.models import AuditAction, AuditLog, SourceDocument, SystemSettings
from extractor.utils import (
    APPLICATION_JSON,
    KNATIVE_MIN_SCALE,
    calculate_file_sha256,
    clean_html_content,
    format_localized_cost,
    generate_curated_zip_bundle,
    get_client_ip,
    get_locale_currency_details,
    query_semantic_knowledge_rag,
    render_markdown_to_html,
)


def _get_dashboard_stats(request):
    """
    Helper to calculate and format dashboard statistics, avoiding duplicate logic
    between DashboardView and DocumentStatusAPIView.
    """
    currency_details = get_locale_currency_details(request)
    now = timezone.now()
    first_of_month = datetime(now.year, now.month, 1, tzinfo=UTC)

    user = request.user
    if user.is_staff or user.is_superuser:
        base_qs = SourceDocument.objects.all()
    else:
        base_qs = SourceDocument.objects.filter(Q(uploaded_by=user) | Q(uploaded_by__isnull=True)).distinct()

    monthly_live = base_qs.filter(created_at__gte=first_of_month).aggregate(total=Sum("cost_usd"))["total"] or Decimal(
        "0.0"
    )

    # Add cost of documents that were deleted this month (persisted in MonthlySpendLog)
    from extractor.models import MonthlySpendLog

    monthly_logged = MonthlySpendLog.total_for_month(now.year, now.month)
    monthly_spent = monthly_live + monthly_logged

    total_spent_usd = base_qs.aggregate(total=Sum("cost_usd"))["total"] or Decimal("0.0")
    total_tokens = base_qs.aggregate(prompt=Sum("input_tokens"), candidates=Sum("output_tokens"))
    prompt_tokens = total_tokens["prompt"] or 0
    cand_tokens = total_tokens["candidates"] or 0
    total_tokens_spent = prompt_tokens + cand_tokens

    # Status stats
    status_stats = base_qs.values("status").annotate(count=Count("id"))
    stats_dict = {choice[0]: 0 for choice in SourceDocument.STATUS_CHOICES}
    for stat in status_stats:
        if stat["status"] in stats_dict:
            stats_dict[stat["status"]] = stat["count"]

    # Total pages
    total_pages = base_qs.filter(status="COMPLETED").aggregate(total=Sum("page_count"))["total"] or 0

    # Budget cap checks
    system_settings_obj = SystemSettings.get_settings()
    budget_cap = Decimal(str(system_settings_obj.monthly_budget_usd))
    budget_exceeded = monthly_spent >= budget_cap

    percent_spent = int((monthly_spent / budget_cap) * 100) if budget_cap > 0 else 0
    if percent_spent > 100:
        percent_spent = 100
    stats_dict["percent_spent"] = percent_spent

    # Format metrics
    formatted_monthly = format_localized_cost(monthly_spent, currency_details)
    formatted_total_spent = format_localized_cost(total_spent_usd, currency_details)
    formatted_cap = f"${budget_cap:.2f} USD"

    return {
        "monthly_spent": monthly_spent,
        "total_spent_usd": total_spent_usd,
        "prompt_tokens": prompt_tokens,
        "candidates_tokens": cand_tokens,
        "total_tokens_spent": total_tokens_spent,
        "stats_dict": stats_dict,
        "total_pages": total_pages,
        "budget_cap": budget_cap,
        "budget_exceeded": budget_exceeded,
        "percent_spent": percent_spent,
        "formatted_monthly_spent": formatted_monthly,
        "formatted_total_spent": formatted_total_spent,
        "formatted_budget_cap": formatted_cap,
        "currency_details": currency_details,
    }


class DashboardView(LoginRequiredMixin, View):
    """
    Renders the unified glassmorphic administrative control dashboard.
    Visualizes token spends, success ratios, and displays a complete search bar
    to interact with the SurrealDB HNSW semantic memory.
    """

    def get(self, request):
        sort_by = request.GET.get("sort_by", "-date")
        allowed_sorts = {
            "name": "title",
            "-name": "-title",
            "cost": "cost_usd",
            "-cost": "-cost_usd",
            "status": "status",
            "-status": "-status",
            "date": "created_at",
            "-date": "-created_at",
        }
        db_sort = allowed_sorts.get(sort_by, "-created_at")

        if request.user.is_staff or request.user.is_superuser:
            docs = SourceDocument.objects.order_by(db_sort)
        else:
            docs = (
                SourceDocument.objects.filter(Q(uploaded_by=request.user) | Q(uploaded_by__isnull=True))
                .distinct()
                .order_by(db_sort)
            )
        stats = _get_dashboard_stats(request)

        # Render lists with pre-calculated formatting
        list_docs = []
        for d in docs[:50]:  # Cap dashboard display to recent 50 entries
            list_docs.append(
                {
                    "obj": d,
                    "formatted_cost": format_localized_cost(d.cost_usd, stats["currency_details"]),
                    "created_at_local": d.created_at.strftime("%Y-%m-%d %H:%M"),
                }
            )

        context = {
            "documents": list_docs,
            "stats": stats["stats_dict"],
            "total_docs_count": docs.count(),
            "total_pages": stats["total_pages"],
            "total_tokens": stats["total_tokens_spent"],
            "prompt_tokens": stats["prompt_tokens"],
            "candidates_tokens": stats["candidates_tokens"],
            "total_spent_usd": stats["total_spent_usd"],
            "formatted_monthly_spent": stats["formatted_monthly_spent"],
            "formatted_budget_cap": stats["formatted_budget_cap"],
            "budget_exceeded": stats["budget_exceeded"],
            "currency_details": stats["currency_details"],
            "sort_by": sort_by,
        }
        return render(request, "extractor/dashboard.html", context)


class UploadView(LoginRequiredMixin, View):
    """
    Handles secure drag-and-drop document uploads with automatic SHA-256
    content de-duplication to guarantee zero wasted storage on identical uploads.
    Supports multi-file batch uploads.
    """

    def _clone_deduplicated_document(self, request, existing_doc, orig_name, file_hash, ip):
        doc = SourceDocument.objects.create(
            file=existing_doc.file,
            original_filename=orig_name,
            file_hash=file_hash,
            status="COMPLETED",
            uploaded_by=request.user,
            language=existing_doc.language,
            author=existing_doc.author,
            title=existing_doc.title,
            document_type=existing_doc.document_type,
            page_count=existing_doc.page_count,
            raw_markdown=existing_doc.raw_markdown,
            refined_markdown=existing_doc.refined_markdown,
            yaml_metadata=existing_doc.yaml_metadata,
            qa_dataset=existing_doc.qa_dataset,
            cost_usd=Decimal("0.00"),  # Cost is zero since no LLM runs!
            semantic_signature=existing_doc.semantic_signature,
            expires_at=timezone.now() + timezone.timedelta(days=int(getattr(settings, "DATA_RETENTION_DAYS", 30))),
        )

        # Gap B-8: clone chunks in SurrealDB instead of pgvector ORM
        from extractor import surreal_db

        try:
            surreal_db.clone_chunks(str(existing_doc.uuid), str(doc.uuid))
        except Exception as clone_err:
            logger.warning("[Upload] SurrealDB chunk clone failed: %s", clone_err)

        # Audit log for cached upload
        from extractor.utils import log_audit_event

        log_audit_event(
            action=AuditAction.UPLOAD_CACHED,
            user=request.user,
            document=doc,
            details=f"File '{orig_name}' uploaded and instantly cached via de-duplication.",
            ip_address=ip,
        )
        return {"status": "cached", "name": orig_name}

    def _retry_existing_failed_document(self, request, existing_doc, orig_name, ip):
        if existing_doc.retry_count >= 3:
            return {
                "status": "error",
                "name": orig_name,
                "error": f"Maximum retry limit of 3 exceeded for file '{orig_name}'.",
            }
        # Reset status and re-enqueue (auto-retry on re-upload!)
        doc_ref = SourceDocument.objects.select_for_update().get(id=existing_doc.id)
        doc_ref.status = "PENDING"
        doc_ref.cost_usd = Decimal("0.00")
        doc_ref.input_tokens = 0
        doc_ref.output_tokens = 0
        doc_ref.error_message = ""
        doc_ref.retry_count += 1
        doc_ref.save()

        # Audit log for retry upload
        from extractor.utils import log_audit_event

        log_audit_event(
            action=AuditAction.UPLOAD,
            user=request.user,
            document=doc_ref,
            details=f"File '{orig_name}' re-uploaded; resetting failed pipeline status and re-enqueuing.",
            ip_address=ip,
        )

        # Dispatch Cloud Tasks background job
        from extractor import cloud_tasks

        transaction.on_commit(
            lambda doc_id=doc_ref.id: cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        )
        return {"status": "success", "name": f"{orig_name} (re-enqueued)"}

    def _create_fresh_document(self, request, uploaded_file, orig_name, file_hash, ip):
        import os

        title_guess = os.path.splitext(orig_name)[0].replace("_", " ").replace("-", " ").strip()
        ext_guess = os.path.splitext(orig_name)[1].replace(".", "").upper()
        if not ext_guess:
            ext_guess = "PDF"

        doc = SourceDocument.objects.create(
            file=uploaded_file,
            original_filename=orig_name,
            file_hash=file_hash,
            status="PENDING",
            uploaded_by=request.user,
            title=title_guess or "Untitled",
            document_type=ext_guess,
        )

        # Audit log for fresh upload
        from extractor.utils import log_audit_event

        log_audit_event(
            action=AuditAction.UPLOAD,
            user=request.user,
            document=doc,
            details=f"File '{orig_name}' uploaded successfully (size: {uploaded_file.size} bytes).",
            ip_address=ip,
        )

        # Dispatch Cloud Tasks background job
        from extractor import cloud_tasks

        transaction.on_commit(lambda doc_id=doc.id: cloud_tasks.enqueue("process_document", {"document_id": doc_id}))
        return {"status": "success", "name": orig_name}

    def _process_single_file(self, request, uploaded_file, ip, processed_hashes):
        orig_name = uploaded_file.name

        # Check size constraints
        if uploaded_file.size > 31457280:
            return {"status": "error", "name": orig_name, "error": f"'{orig_name}' exceeds maximum 30MB constraint."}

        # 1. Compute SHA-256 check
        file_hash = calculate_file_sha256(uploaded_file)

        # Check batch duplicate
        if file_hash in processed_hashes:
            return {
                "status": "error",
                "name": orig_name,
                "error": f"File '{orig_name}' is a duplicate of another file in this batch.",
            }

        processed_hashes.add(file_hash)

        # 2. Check GCS deduplication
        # Prioritize matching an existing document owned by the current user
        existing_user_doc = SourceDocument.objects.filter(file_hash=file_hash, uploaded_by=request.user).first()
        if existing_user_doc:
            existing_doc = existing_user_doc
        else:
            # Prioritize matching COMPLETED documents first for instant-caching from other users
            existing_doc = SourceDocument.objects.filter(file_hash=file_hash, status="COMPLETED").first()
            if not existing_doc:
                # Check for any in-progress or failed ones from other users
                existing_doc = SourceDocument.objects.filter(file_hash=file_hash).first()

        try:
            with transaction.atomic():
                if existing_doc:
                    if existing_doc.status == "COMPLETED":
                        if existing_doc.uploaded_by == request.user:
                            # User already has this file in their library! No need to clone/copy it.
                            logger.info(
                                "[Deduplication] User already has completed document with hash %s. Reusing without copy.",
                                file_hash,
                            )
                            return {"status": "cached", "name": orig_name}

                        # Deduplication Match from other user! Clone it.
                        logger.info(
                            "[Deduplication] Match found for file hash %s. Skipping physical rewrite.", file_hash
                        )
                        return self._clone_deduplicated_document(request, existing_doc, orig_name, file_hash, ip)
                    elif existing_doc.status in ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]:
                        # File is already processing
                        return {
                            "status": "error",
                            "name": orig_name,
                            "error": f"File '{orig_name}' is already being processed by the background worker.",
                        }
                    elif existing_doc.status == "FAILED":
                        return self._retry_existing_failed_document(request, existing_doc, orig_name, ip)

                # Fresh File upload
                return self._create_fresh_document(request, uploaded_file, orig_name, file_hash, ip)
        except Exception as e:
            return {"status": "error", "name": orig_name, "error": f"Error processing '{orig_name}': {e!s}"}

    def post(self, request):
        uploaded_files = request.FILES.getlist("file")
        if not uploaded_files:
            messages.error(request, "No file uploaded.")
            return redirect("dashboard")

        ip = get_client_ip(request)
        successful_uploads = []
        cached_uploads = []
        errors = []
        processed_hashes = set()

        for uploaded_file in uploaded_files:
            res = self._process_single_file(request, uploaded_file, ip, processed_hashes)
            if res["status"] == "success":
                successful_uploads.append(res["name"])
            elif res["status"] == "cached":
                cached_uploads.append(res["name"])
            else:
                errors.append(res["error"])

        # Consolidated messages
        if successful_uploads:
            messages.success(request, f"Successfully uploaded and queued: {', '.join(successful_uploads)}.")
        if cached_uploads:
            messages.success(
                request, f"Instantly cached from deduplication ($0 USD cost saved): {', '.join(cached_uploads)}."
            )
        if errors:
            messages.error(request, f"Some uploads failed: {'; '.join(errors)}")

        return redirect("dashboard")


class DocumentDetailView(LoginRequiredMixin, View):
    """
    Renders details for a single document, incorporating a dual-screen Markdown Split-Editor
    and interactive Q&A dataset panels.
    """

    def get(self, request, doc_uuid):
        doc = get_object_or_404(SourceDocument, uuid=doc_uuid)
        # Check standard user access boundary (only uploader, staff, or system documents with no uploader can access)
        if not (
            request.user.is_staff
            or request.user.is_superuser
            or doc.uploaded_by is None
            or doc.uploaded_by == request.user
        ):
            messages.error(request, "Permission denied to view this document.")
            return redirect("dashboard")

        currency_details = get_locale_currency_details(request)

        # Render markdown to HTML for presentation
        rendered_raw = render_markdown_to_html(doc.raw_markdown) if doc.raw_markdown else ""
        rendered_refined = render_markdown_to_html(doc.refined_markdown) if doc.refined_markdown else ""

        # Live cost localization formatting
        formatted_cost = format_localized_cost(doc.cost_usd, currency_details)

        parsed_yaml = {}
        if doc.yaml_metadata:
            try:
                import yaml

                from extractor.tasks import _sanitise_yaml_block

                try:
                    meta_raw = yaml.safe_load(doc.yaml_metadata)
                except Exception:
                    meta_raw = yaml.safe_load(_sanitise_yaml_block(doc.yaml_metadata))
                if isinstance(meta_raw, dict):
                    parsed_yaml = {str(k).strip().lower(): v for k, v in meta_raw.items()}
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


class DocumentSaveView(LoginRequiredMixin, View):
    """
    Saves edited Markdown text straight from the split pane editor
    and refreshes the physical file back to GCS or disk.
    """

    def post(self, request, doc_uuid):
        doc = get_object_or_404(SourceDocument, uuid=doc_uuid)
        # Check standard user access boundary (only uploader or staff can edit)
        if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
            messages.error(request, "Permission denied to modify this document.")
            return redirect("dashboard")

        new_markdown = request.POST.get("refined_markdown", "")
        new_title = request.POST.get("title")
        new_author = request.POST.get("author")
        new_language = request.POST.get("language")

        # Sanitize HTML elements safely
        sanitized_markdown = clean_html_content(new_markdown)

        with transaction.atomic():
            doc = SourceDocument.objects.select_for_update().get(uuid=doc_uuid)
            doc.refined_markdown = sanitized_markdown
            if new_title is not None:
                doc.title = new_title.strip()
            if new_author is not None:
                doc.author = new_author.strip()
            if new_language is not None:
                doc.language = new_language.strip()
            doc.save()

            # Log the document edit event
            from extractor.utils import log_audit_event

            log_audit_event(
                action=AuditAction.DOCUMENT_EDITED,
                user=request.user,
                document=doc,
                details=f"Document '{doc.original_filename}' content modified and re-embedding queued.",
                ip_address=get_client_ip(request),
            )

            # Re-embed after edit to keep SurrealDB HNSW memory in sync with editor changes (Gap B-8)
            from extractor import cloud_tasks

            transaction.on_commit(
                lambda doc_id=doc.id: cloud_tasks.enqueue("reembed_document", {"document_id": doc_id})
            )

        messages.success(request, "Changes saved and re-indexing queued successfully!")
        return redirect("document_detail", doc_uuid=doc_uuid)


class DocumentDeleteView(LoginRequiredMixin, View):
    """
    Reference-counted document deletion.
    If multiple DB entries point to the same content address (SHA-256),
    the GCS file is preserved. Otherwise, it is deleted cleanly.
    """

    def post(self, request, doc_uuid):
        doc = get_object_or_404(SourceDocument, uuid=doc_uuid)
        # Check standard user access boundary (only uploader or staff can delete)
        if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
            messages.error(request, "Permission denied to delete this document.")
            return redirect("dashboard")

        file_hash = doc.file_hash
        orig_name = doc.original_filename

        # Count shared hash pointers
        shared_references = SourceDocument.objects.filter(file_hash=file_hash).exclude(id=doc.id).count()
        ip = get_client_ip(request)

        with transaction.atomic():
            # Create audit log record before deleting to preserve User/Document foreign key context
            from extractor.utils import log_audit_event

            log_audit_event(
                action=AuditAction.DELETE,
                user=request.user,
                document=doc,
                details=f"Deleted document '{orig_name}' (Title: {doc.title}). Shared references remaining: {shared_references}.",
                ip_address=ip,
            )

            # Flip delete order: delete file first if shared_references == 0
            if shared_references == 0:
                try:
                    doc.file.delete(save=False)  # Safely deletes physical object from GCS
                    logger.info("[De-duplication Delete] Purged file hash %s physically.", file_hash)
                except Exception as e:
                    logger.warning(
                        "[De-duplication Delete] Failed to physically delete file for hash %s: %s", file_hash, e
                    )

            doc.delete()  # ORM cascade — SurrealDB chunk deletion via post_delete signal

            if shared_references != 0:
                logger.info(
                    "[De-duplication Delete] Preserved file hash %s (Shared with %s entries).",
                    file_hash,
                    shared_references,
                )

        # Gap B-8: purge SurrealDB chunks outside atomic block (surreal_db has its own atomicity)
        from django.core.files.storage import default_storage

        from extractor import surreal_db

        try:
            surreal_db.delete_chunks(str(doc.uuid))
        except Exception as chunk_err:
            logger.warning("[Delete] SurrealDB chunk deletion failed for %s: %s", doc.uuid, chunk_err)

        try:
            chunks_json_path = f"chunks/{doc.uuid}.json"
            if default_storage.exists(chunks_json_path):
                default_storage.delete(chunks_json_path)
        except Exception as storage_err:
            logger.warning("[Delete] Storage chunk JSON deletion failed for %s: %s", doc.uuid, storage_err)

        messages.success(request, f"Document '{orig_name}' deleted successfully.")
        return redirect("dashboard")


class DocumentPurgeAllView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Enables instant GDPR 'Right to be Forgotten' data minimization.
    Deletes all records and physically flushes matching GCS files cleanly.
    """

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.is_staff

    def post(self, request):
        docs = SourceDocument.objects.all()
        ip = get_client_ip(request)

        # Log the global purge all event first
        from extractor.utils import log_audit_event

        log_audit_event(
            action=AuditAction.PURGE_ALL,
            user=request.user,
            details=f"Purged all documents and associated semantic memory vector embeddings. Count: {docs.count()}.",
            ip_address=ip,
        )

        for doc in docs:
            # Check if this file is the last one pointing to this hash
            shared = SourceDocument.objects.filter(file_hash=doc.file_hash).exclude(id=doc.id).count()
            if shared == 0:
                try:
                    doc.file.delete(save=False)
                except Exception as e:
                    logger.warning("[Purge All] Failed to delete file for %s: %s", doc.title, e)
            doc.delete()

        # Flush SurrealDB entirely to delete any leftover chunks, metadata, caches
        from extractor import surreal_db

        try:
            surreal_db.purge_all()
        except Exception as exc:
            logger.warning("[Purge All] Failed to flush SurrealDB database: %s", exc)

        # Delete all chunks JSON files in storage
        try:
            from django.core.files.storage import default_storage

            dirs, files = default_storage.listdir("chunks")
            for f in files:
                default_storage.delete(f"chunks/{f}")
            logger.info("[Purge All] Cleaned all chunk JSON files from GCS storage.")
        except Exception as storage_err:
            logger.warning("[Purge All] Failed to clean chunks folder in GCS: %s", storage_err)

        messages.success(request, "Reset Memory Complete: Purged all documents and vector embeddings.")
        return redirect("dashboard")


class DocumentRAGSearchView(LoginRequiredMixin, View):
    """
    AJAX endpoint running global semantic search.
    Embeds query, searches Supabase pgvector, and generates answers.
    """

    def get(self, request):
        query = request.GET.get("q", "").strip()
        if not query:
            return JsonResponse({"error": "Empty search query."}, status=400)

        document_ids_str = request.GET.get("document_ids", "").strip()
        document_ids = None
        if document_ids_str:
            try:
                document_ids = [int(i.strip()) for i in document_ids_str.split(",") if i.strip()]
            except ValueError:
                pass

        try:
            results = query_semantic_knowledge_rag(
                query,
                document_ids=document_ids,
                top_k=5,
                user=request.user,
            )
            # Render markdown answer safely to HTML
            results["answer_html"] = render_markdown_to_html(results["answer"])
            return JsonResponse(results)
        except ValueError as e:
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            return JsonResponse({"error": f"Search Error: {e!s}"}, status=500)


class ExportZipView(LoginRequiredMixin, View):
    """
    Curates and builds dynamic ZIP bundle directories sorted by Language and Author,
    bundling metadata manifests and a single 'master_notebooklm_source.md' document.
    """

    def post(self, request):
        document_ids = request.POST.getlist("selected_documents")
        if not document_ids:
            messages.error(request, "No documents selected for export.")
            return redirect("dashboard")

        try:
            zip_data = generate_curated_zip_bundle(document_ids, user=request.user)
            response = HttpResponse(zip_data, content_type="application/zip")
            response["Content-Disposition"] = (
                f'attachment; filename="curated_notebooklm_export_{timezone.now().strftime("%Y%m%d%H%M")}.zip"'
            )
            return response
        except Exception as e:
            messages.error(request, f"Export failure: {e!s}")
            return redirect("dashboard")


class BulkDocumentActionView(LoginRequiredMixin, View):
    """
    Handles bulk operations (Delete or Reprocess/Restart) on selected documents.
    """

    def post(self, request):
        action = request.POST.get("action")
        document_ids = request.POST.getlist("selected_documents")
        if not document_ids:
            messages.error(request, "No documents selected.")
            return redirect("dashboard")

        if action == "restart":
            # Restart bulk curation
            from extractor import cloud_tasks
            from extractor.models import SourceDocument

            docs = SourceDocument.objects.filter(id__in=document_ids)
            restarted_count = 0
            for doc in docs:
                # Access boundary check
                if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
                    continue

                if doc.status in ["FAILED", "COMPLETED"]:
                    doc.status = "PENDING"
                    doc.retry_count = 0
                    doc.error_message = ""
                    doc.save()
                    # Enqueue inside transaction.on_commit to ensure task runs after commit
                    transaction.on_commit(
                        lambda d_id=doc.id: cloud_tasks.enqueue("process_document", {"document_id": d_id})
                    )
                    restarted_count += 1
            messages.success(request, f"Successfully queued {restarted_count} tasks for reprocessing.")

        elif action == "delete":
            # Bulk delete documents
            from django.core.files.storage import default_storage

            from extractor import surreal_db
            from extractor.models import AuditAction, SourceDocument
            from extractor.utils import get_client_ip, log_audit_event

            if request.user.is_staff or request.user.is_superuser:
                docs = SourceDocument.objects.filter(id__in=document_ids)
            else:
                docs = SourceDocument.objects.filter(id__in=document_ids, uploaded_by=request.user)

            docs = list(docs)
            file_hashes = {doc.file_hash for doc in docs if doc.file_hash}

            hash_ref_counts = dict(
                SourceDocument.objects.filter(file_hash__in=file_hashes)
                .values("file_hash")
                .annotate(count=Count("id"))
                .values_list("file_hash", "count")
            )

            deleted_count = 0
            for doc in docs:
                file_hash = doc.file_hash
                orig_name = doc.original_filename

                total_refs = hash_ref_counts.get(file_hash, 0)
                shared_references = max(0, total_refs - 1)

                if file_hash in hash_ref_counts:
                    hash_ref_counts[file_hash] = max(0, total_refs - 1)

                with transaction.atomic():
                    log_audit_event(
                        action=AuditAction.DELETE,
                        user=request.user,
                        document=doc,
                        details=f"Bulk Deleted document '{orig_name}' (Title: {doc.title}). Shared references remaining: {shared_references}.",
                        ip_address=get_client_ip(request),
                    )

                    if shared_references == 0:
                        try:
                            doc.file.delete(save=False)
                        except Exception as e:
                            logger.warning("[Bulk Delete] Failed to physically delete file: %s", e)

                    doc.delete()

                # SurrealDB chunks cleanup
                try:
                    surreal_db.delete_chunks(str(doc.uuid))
                except Exception as chunk_err:
                    logger.warning("[Bulk Delete] SurrealDB chunk deletion failed for %s: %s", doc.uuid, chunk_err)

                # Storage JSON chunks cleanup
                try:
                    chunks_json_path = f"chunks/{doc.uuid}.json"
                    if default_storage.exists(chunks_json_path):
                        default_storage.delete(chunks_json_path)
                except Exception as storage_err:
                    logger.warning("[Bulk Delete] Storage chunk JSON deletion failed for %s: %s", doc.uuid, storage_err)

                deleted_count += 1
            messages.success(request, f"Successfully deleted {deleted_count} documents from the repository.")

        else:
            messages.error(request, f"Invalid bulk action: {action}")

        return redirect("dashboard")


class SaveSettingsView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Saves the configurable Monthly Budget, Source Library URI, LLM Model, and Custom Public Domains dynamically in SystemSettings.
    """

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.is_staff

    def post(self, request):
        monthly_budget_usd = request.POST.get("monthly_budget_usd", "10.00").strip()
        selected_model = request.POST.get("selected_model", "auto").strip()
        currency = request.POST.get("currency", "auto").strip()
        csrf_trusted_origins = request.POST.get("csrf_trusted_origins", "").strip()
        openrouter_api_key = request.POST.get("openrouter_api_key", "").strip()

        # Strict validation whitelist for SystemSettings selected_model to prevent unauthorized/expensive models
        ALLOWED_MODELS = {
            "auto",
            "google/gemini-3.5-flash",
            "google/gemini-3.1-flash-lite",
            "google/gemini-3.1-flash",
            "google/gemini-2.5-flash-lite",
            "google/gemini-2.5-flash",
            "google/gemini-1.5-flash",
            "meta-llama/llama-3-8b-instruct:free",
            "google/gemma-2-9b-it:free",
            "qwen/qwen-2-7b-instruct:free",
        }
        if selected_model not in ALLOWED_MODELS:
            messages.error(request, "Invalid model selection.")
            return redirect("dashboard")

        if currency not in {"auto", "USD", "IDR", "SAR"}:
            messages.error(request, "Invalid currency selection.")
            return redirect("dashboard")

        try:
            budget_val = Decimal(monthly_budget_usd)
            if budget_val < 0:
                raise ValueError("Budget cannot be negative.")
        except Exception:
            messages.error(request, "Invalid budget value provided. Must be a valid positive number.")
            return redirect("dashboard")

        settings_obj = SystemSettings.get_settings()
        settings_obj.monthly_budget_usd = budget_val
        settings_obj.selected_model = selected_model
        settings_obj.currency = currency
        settings_obj.csrf_trusted_origins = csrf_trusted_origins

        # Only overwrite the API key if it's not the masked placeholder
        if openrouter_api_key and openrouter_api_key != "••••••••••••••••":
            settings_obj.openrouter_api_key = openrouter_api_key
        elif not openrouter_api_key:
            # If the user explicitly cleared the input, remove the key
            settings_obj.openrouter_api_key = ""

        settings_obj.save()

        messages.success(request, "System settings updated successfully!")
        return redirect("dashboard")


class DocumentStatusAPIView(LoginRequiredMixin, View):
    """
    Fast, lightweight JSON API view returning the status of all active/recent documents,
    along with real-time dashboard statistics (monthly budget spent, token counts, success ratio)
    to enable live dynamic updates without full-page reloads.
    """

    def get(self, request):
        if request.user.is_staff or request.user.is_superuser:
            docs = SourceDocument.objects.order_by("-created_at")
        else:
            docs = (
                SourceDocument.objects.filter(Q(uploaded_by=request.user) | Q(uploaded_by__isnull=True))
                .distinct()
                .order_by("-created_at")
            )
        stats = _get_dashboard_stats(request)

        # Build status map for all documents (limited to recent 100)
        docs_list = []
        for d in docs[:100]:
            docs_list.append(
                {
                    "id": d.id,
                    "uuid": str(d.uuid),
                    "status": d.status,
                    "status_display": d.get_status_display(),
                    "title": d.title,
                    "cost_usd": float(d.cost_usd),
                    "formatted_cost": format_localized_cost(d.cost_usd, stats["currency_details"]),
                    "input_tokens": d.input_tokens,
                    "output_tokens": d.output_tokens,
                    "language": d.language or "Unknown",
                    "author": d.author or "Unknown",
                    "page_count": d.page_count or 0,
                    "created_at_local": d.created_at.strftime("%Y-%m-%d %H:%M"),
                }
            )

        data = {
            "documents": docs_list,
            "stats": {
                "COMPLETED": stats["stats_dict"].get("COMPLETED", 0),
                "PENDING": stats["stats_dict"].get("PENDING", 0),
                "EXTRACTING": stats["stats_dict"].get("EXTRACTING", 0),
                "REFINING": stats["stats_dict"].get("REFINING", 0),
                "EMBEDDING": stats["stats_dict"].get("EMBEDDING", 0),
                "FAILED": stats["stats_dict"].get("FAILED", 0),
                "percent_spent": stats["percent_spent"],
                "total_docs_count": docs.count(),
                "total_pages": stats["total_pages"],
                "total_tokens": stats["total_tokens_spent"],
                "prompt_tokens": stats["prompt_tokens"],
                "candidates_tokens": stats["candidates_tokens"],
                "monthly_spent": float(stats["monthly_spent"]),
                "formatted_monthly_spent": stats["formatted_monthly_spent"],
                "formatted_total_spent": stats["formatted_total_spent"],
                "formatted_budget_cap": stats["formatted_budget_cap"],
                "budget_exceeded": stats["budget_exceeded"],
            },
        }
        return JsonResponse(data)


class DocumentRetryView(LoginRequiredMixin, View):
    """
    Resets the document status, costs, and token spent metrics,
    and re-queues the process_document_task background job.

    Response contract:
    - AJAX/JSON requests (detected via X-Requested-With: XMLHttpRequest or
      Accept: application/json headers) receive a JsonResponse.
    - Standard form POST requests receive a HTTP redirect to the dashboard view.
    """

    def post(self, request, doc_uuid):
        doc = get_object_or_404(SourceDocument, uuid=doc_uuid)
        # Check standard user access boundary (only uploader or staff can retry)
        if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
            if (
                request.headers.get("x-requested-with") == "XMLHttpRequest"
                or request.headers.get("accept") == APPLICATION_JSON
            ):
                return JsonResponse({"error": "Permission denied to retry this document."}, status=403)
            messages.error(request, "Permission denied to retry this document.")
            return redirect("dashboard")

        is_restart = doc.status == "COMPLETED"

        if not is_restart and doc.retry_count >= 3:
            if (
                request.headers.get("x-requested-with") == "XMLHttpRequest"
                or request.headers.get("accept") == APPLICATION_JSON
            ):
                return JsonResponse({"error": "Maximum retry limit of 3 exceeded for this document."}, status=400)
            messages.error(request, f"Maximum retry limit of 3 exceeded for document: {doc.original_filename}")
            return redirect("dashboard")

        with transaction.atomic():
            doc_ref = SourceDocument.objects.select_for_update().get(id=doc.id)
            doc_ref.status = "PENDING"
            doc_ref.cost_usd = Decimal("0.00")
            doc_ref.input_tokens = 0
            doc_ref.output_tokens = 0
            doc_ref.error_message = ""
            if is_restart:
                doc_ref.retry_count = 0
            else:
                doc_ref.retry_count += 1
            doc_ref.save()

        # Re-dispatch the background task via Cloud Tasks
        from extractor import cloud_tasks

        cloud_tasks.enqueue("process_document", {"document_id": doc.id})

        if (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or request.headers.get("accept") == APPLICATION_JSON
        ):
            return JsonResponse({"status": "success", "message": "Curation pipeline re-enqueued."})

        messages.success(request, f"Re-enqueued curation pipeline for document: {doc.title or doc.original_filename}")
        return redirect("dashboard")


class AuditLogListView(LoginRequiredMixin, View):
    """
    Renders the secure, premium glassmorphic system audit trail.
    Standard users see only their own logs, while superusers and staff members see all logs.
    Enables quick action filtering, search index, and complete traceability.
    """

    def get(self, request):
        is_staff_or_superuser = request.user.is_superuser or request.user.is_staff
        if is_staff_or_superuser:
            logs = AuditLog.objects.all().select_related("user", "document")
        else:
            logs = AuditLog.objects.filter(user=request.user).select_related("user", "document")

        # Filtering parameters
        action_filter = request.GET.get("action", "").strip()
        user_query = request.GET.get("user", "").strip()
        search_query = request.GET.get("q", "").strip()

        if action_filter:
            logs = logs.filter(action=action_filter)
        if is_staff_or_superuser and user_query:
            logs = logs.filter(user__username__icontains=user_query)
        if search_query:
            logs = logs.filter(
                Q(details__icontains=search_query)
                | Q(ip_address__icontains=search_query)
                | Q(document__original_filename__icontains=search_query)
                | Q(document__title__icontains=search_query)
            )

        action_choices = [
            (AuditAction.LOGIN, "Login"),
            (AuditAction.LOGOUT, "Logout"),
            (AuditAction.UPLOAD, "Upload Fresh"),
            (AuditAction.UPLOAD_CACHED, "Upload Cached"),
            (AuditAction.EXTRACTION_START, "Pipeline Started"),
            (AuditAction.EXTRACTION_COMPLETED, "Pipeline Completed"),
            (AuditAction.EXTRACTION_FAILED, "Pipeline Failed"),
            (AuditAction.DELETE, "Delete Document"),
            (AuditAction.PURGE_ALL, "Purge All Records"),
            (AuditAction.DOCUMENT_EDITED, "Document Edited"),
            (AuditAction.DOCUMENT_REQUEUED, "Document Requeued"),
            (AuditAction.SYSTEM_CONTROL, "System Control"),
        ]

        context = {
            "logs": logs.distinct()[:200],  # Limit display for UI performance
            "action_choices": action_choices,
            "selected_action": action_filter,
            "selected_user": user_query if is_staff_or_superuser else "",
            "search_query": search_query,
        }
        return render(request, "extractor/audit_logs.html", context)


import logging

logger = logging.getLogger(__name__)


class DeploymentControllerView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Centralized Deployment and Cost Console allowing admins/staff to:
    - View active Cloud Run configurations and scaling limits.
    - Change worker service mode on-demand ("Hibernating" min_instances=0, max_instances=0,
      "On-Demand" min_instances=0, max_instances=5, "Always-On" min_instances=1, max_instances=5).
    - Trigger QA system diagnostics.
    - View live aggregated container logs.
    """

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.is_staff

    def get(self, request):
        from extractor.deployment import (
            extract_knative_scaling,
            get_gcp_project_details,
            get_service_config,
            get_service_logs,
        )

        details = get_gcp_project_details()
        project_id = details["project_id"]
        region = details["region"]

        # Default fallback values for local development
        worker_config = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            KNATIVE_MIN_SCALE: "0",
                            "autoscaling.knative.dev/maxScale": "0",
                        }
                    }
                }
            }
        }
        web_config = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            KNATIVE_MIN_SCALE: "1",
                            "autoscaling.knative.dev/maxScale": "5",
                        }
                    }
                }
            }
        }
        worker_logs = []
        web_logs = []
        worker_service_name = "data-extractor-worker"
        gcp_active = False

        try:
            worker_real = get_service_config("data-extractor-worker")
            if worker_real:
                worker_config = worker_real
                gcp_active = True
        except Exception as e:
            logger.warning(f"Could not load worker config from GCP (local fallback): {e}")
            try:
                web_real = get_service_config("data-extractor-web")
                if web_real:
                    worker_service_name = "data-extractor-web"
                    worker_config = web_real
                    gcp_active = True
            except Exception as e_web:
                logger.warning(f"Could not load web config either: {e_web}")

        try:
            web_real = get_service_config("data-extractor-web")
            if web_real:
                web_config = web_real
        except Exception as e:
            logger.warning(f"Could not load web config from GCP (local fallback): {e}")

        # Extract scaling values
        worker_min, worker_max = extract_knative_scaling(worker_config, 0, 0)
        web_min, web_max = extract_knative_scaling(web_config, 1, 5)

        # Determine current mode
        if worker_min == 0 and worker_max <= 1:
            current_mode = "hibernate"
        elif worker_min == 0 and worker_max > 1:
            current_mode = "on-demand"
        else:
            current_mode = "always-on"

        # Fetch logs (last 50 rows)
        try:
            worker_logs = get_service_logs(worker_service_name, limit=50)
        except Exception as e:
            worker_logs = [{"timestamp": "", "message": f"Log retrieval error: {e}", "severity": "ERROR"}]

        try:
            web_logs = get_service_logs("data-extractor-web", limit=50)
        except Exception as e:
            web_logs = [{"timestamp": "", "message": f"Log retrieval error: {e}", "severity": "ERROR"}]

        # Check SurrealDB health status
        from extractor.surreal_db import check_health

        surreal_ok = check_health()

        context = {
            "project_id": project_id,
            "region": region,
            "gcp_active": gcp_active,
            "worker_min": worker_min,
            "worker_max": worker_max,
            "web_min": web_min,
            "web_max": web_max,
            "current_mode": current_mode,
            "worker_logs": worker_logs,
            "web_logs": web_logs,
            "surreal_health": "ONLINE" if surreal_ok else "OFFLINE",
        }
        return render(request, "extractor/deployment_controller.html", context)

    def post(self, request):
        from extractor.deployment import get_service_config, update_service_scale

        mode = request.POST.get("mode", "").strip().lower()

        if mode == "hibernate":
            min_scale, max_scale = 0, 1
            mode_display = "Hibernating ($0.00 Running Cost)"
        elif mode == "on-demand":
            min_scale, max_scale = 0, 5
            mode_display = "On-Demand Serverless Scaling"
        elif mode == "always-on":
            min_scale, max_scale = 1, 5
            mode_display = "Always-On Performance Mode"
        else:
            messages.error(request, "Invalid deployment scaling mode selected.")
            return redirect("deployment_controller")

        # Resolve which service to scale on GCP
        target_service = "data-extractor-worker"
        try:
            get_service_config("data-extractor-worker")
        except Exception:
            try:
                get_service_config("data-extractor-web")
                target_service = "data-extractor-web"
            except Exception as exc:
                logger.debug("Failed to check fallback service data-extractor-web: %s", exc)

        try:
            update_service_scale(target_service, min_scale, max_scale)
            messages.success(request, f"Successfully toggled scaling mode of {target_service} to {mode_display}!")
            # Add audit log for this operational action
            from extractor.utils import log_audit_event

            log_audit_event(
                action=AuditAction.SYSTEM_CONTROL,
                user=request.user,
                details=f"Admins toggled worker scaling mode to '{mode}' (minScale: {min_scale}, maxScale: {max_scale}).",
                ip_address=request.META.get("REMOTE_ADDR"),
            )

        except Exception as e:
            messages.error(request, f"Failed to update Cloud Run scaling settings on GCP: {e!s}")

        return redirect("deployment_controller")


def register_view(request):
    """
    Handles new user signups via Supabase Auth.
    """
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")

        if not email or not password:
            messages.error(request, "Email and Password are required.")
            return render(request, "extractor/register.html")

        if password != confirm_password:
            messages.error(request, "Passwords do not match.")
            return render(request, "extractor/register.html")

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        if not supabase_url or not supabase_key:
            messages.error(request, "Supabase integration is not configured. Local registration is disabled.")
            return render(request, "extractor/register.html")

        # Reject reserved/system emails to prevent privilege escalation
        from urllib.parse import urlparse

        parsed = urlparse(supabase_url)
        domain = parsed.netloc if parsed.netloc else "example.com"

        email_lower = email.lower()
        if email_lower.startswith("admin@") or email_lower.endswith(f"@{domain}"):
            messages.error(request, "Registration of administrative or system email addresses is not permitted.")
            return render(request, "extractor/register.html")

        # Validate email format
        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            messages.error(request, "Invalid email format.")
            return render(request, "extractor/register.html")

        import json
        import urllib.parse
        import urllib.request

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        url = f"{supabase_url.rstrip('/')}/auth/v1/signup?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/login')}"
        from extractor.utils import validate_url_scheme

        try:
            validate_url_scheme(url)
            headers = {"apikey": supabase_key, "Content-Type": "application/json"}
            payload = json.dumps({"email": email, "password": password}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5):  # nosec B310
                messages.success(request, "Registration successful! Please check your email for the activation link.")
                return redirect("login")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                err_msg = json.loads(body).get("msg") or json.loads(body).get("error_description") or body
            except Exception:
                err_msg = body
            messages.error(request, f"Supabase Signup Failed: {err_msg}")
        except Exception as e:
            messages.error(request, f"Network error during registration: {e!s}")

    return render(request, "extractor/register.html")


def forgot_password_view(request):
    """
    Dispatches a password recovery email via Supabase Auth.
    """
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        if not email:
            messages.error(request, "Email is required.")
            return render(request, "extractor/forgot_password.html")

        # Validate email format
        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            messages.error(request, "Invalid email format.")
            return render(request, "extractor/forgot_password.html")

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        if not supabase_url or not supabase_key:
            messages.error(request, "Supabase integration is not configured.")
            return render(request, "extractor/forgot_password.html")

        import json
        import urllib.parse
        import urllib.request

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        url = f"{supabase_url.rstrip('/')}/auth/v1/recover?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/reset-password-confirm')}"
        from extractor.utils import validate_url_scheme

        try:
            validate_url_scheme(url)
            headers = {"apikey": supabase_key, "Content-Type": "application/json"}
            payload = json.dumps({"email": email}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=5):  # nosec B310
                messages.success(request, "Password recovery link has been sent! Please check your email inbox.")
                return redirect("login")
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8")
            try:
                err_msg = json.loads(body).get("msg") or json.loads(body).get("error_description") or body
            except Exception:
                err_msg = body
            messages.error(request, f"Supabase Recovery Failed: {err_msg}")
        except Exception as e:
            messages.error(request, f"Network error: {e!s}")

    return render(request, "extractor/forgot_password.html")


def reset_password_confirm_view(request):
    """
    Renders the password update confirmation view.
    Actual update is handled via client-side Javascript using the saved recovery token.
    """
    supabase_url = getattr(settings, "SUPABASE_URL", "")
    supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

    context = {
        "supabase_url": supabase_url,
        "supabase_key": supabase_key,
    }
    return render(request, "extractor/reset_password_confirm.html", context)
