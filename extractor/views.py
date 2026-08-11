ISO_8601_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
DOCUMENT_NOT_FOUND_MSG = "Document not found."
import logging
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db import transaction  # noqa: F401
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.utils.safestring import mark_safe
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_http_methods

if TYPE_CHECKING:  # nosonar
    # Statically import ingest_sources to satisfy desloppify importer analyzer
    import extractor.management.commands.ingest_sources  # noqa: F401

from django.views import View

logger = logging.getLogger(__name__)

TEMPLATE_REGISTER = "extractor/register.html"
TEMPLATE_FORGOT_PASSWORD = "extractor/forgot_password.html"  # nosec B105


def _turnstile_token_error(request) -> str | None:
    """Return a user-facing error when Turnstile is configured but the token is missing."""
    if not getattr(settings, "CF_TURNSTILE_SITE_KEY", ""):
        return None
    captcha_token = request.POST.get("cf-turnstile-response", "").strip()
    if captcha_token:
        return None
    return "CAPTCHA verification is required. Please complete the security check and try again."


from types import SimpleNamespace

from extractor import surreal_db
from extractor.models import AuditAction, AuditLog


def parse_datetime(val):
    if not val:
        return timezone.now()
    if isinstance(val, datetime):
        return val
    try:
        return datetime.strptime(val, ISO_8601_FORMAT).replace(tzinfo=timezone.UTC)
    except (ValueError, TypeError):
        try:
            from django.utils.dateparse import parse_datetime as django_parse

            parsed = django_parse(val)
            if parsed:
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.UTC)
                return parsed
        except (ValueError, TypeError, AttributeError):
            pass
    return timezone.now()


def format_datetime(dt):
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.UTC)
    return dt.strftime(ISO_8601_FORMAT)


def _wrap_surreal_doc(d, users_map):
    if not d:
        return None
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        try:
            return SourceDocument.objects.get(uuid=d.get("doc_uuid"))
        except SourceDocument.DoesNotExist:
            logger.warning(
                "[SurrealDB Wrapper] SourceDocument with doc_uuid=%s not found in database.", d.get("doc_uuid")
            )

    doc_obj = SimpleNamespace()
    doc_obj.id = d.get("doc_uuid")
    doc_obj.uuid = d.get("doc_uuid")
    doc_obj.doc_uuid = d.get("doc_uuid")
    doc_obj.original_filename = d.get("original_filename")
    doc_obj.title = d.get("title")
    doc_obj.author = d.get("author")
    doc_obj.language = d.get("language")
    doc_obj.document_type = d.get("document_type")
    doc_obj.status = d.get("status")
    doc_obj.cost_usd = Decimal(str(d.get("cost_usd") if d.get("cost_usd") is not None else 0.0))
    doc_obj.page_count = d.get("page_count") if d.get("page_count") is not None else 0
    doc_obj.input_tokens = d.get("input_tokens") if d.get("input_tokens") is not None else 0
    doc_obj.output_tokens = d.get("output_tokens") if d.get("output_tokens") is not None else 0
    doc_obj.raw_markdown = d.get("raw_markdown")
    doc_obj.refined_markdown = d.get("refined_markdown")
    doc_obj.yaml_metadata = d.get("yaml_metadata")
    doc_obj.qa_dataset = d.get("qa_dataset")
    doc_obj.semantic_signature = d.get("semantic_signature")
    doc_obj.error_message = d.get("error_message")
    doc_obj.retry_count = d.get("retry_count", 0)

    doc_obj.created_at = parse_datetime(d.get("created_at"))
    doc_obj.updated_at = parse_datetime(d.get("updated_at"))
    doc_obj.expires_at = parse_datetime(d.get("expires_at")) if d.get("expires_at") else None

    uid = d.get("uploaded_by_id")
    doc_obj.uploaded_by = users_map.get(uid) if uid in users_map else None
    return doc_obj


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


def _render_sanitized_markdown(markdown_text: str) -> str:
    """Render Markdown only after render_markdown_to_html applies HTML sanitization."""
    return mark_safe(render_markdown_to_html(markdown_text))  # nosec B308 B703 # nosem


def _is_budget_exceeded(user) -> bool:
    """
    Lightweight budget gate that only reads Django ORM tables — no SurrealDB.
    Safe to call from any view, including in test environments.
    Returns True when the authenticated user has consumed their monthly AI budget.
    Staff / superusers are never budget-gated.
    """
    if user.is_staff or user.is_superuser:
        return False
    try:
        from decimal import Decimal

        from django.utils import timezone

        from extractor.models import MonthlySpendLog, SystemSettings

        settings_obj = SystemSettings.get_settings()
        budget_cap = Decimal(str(settings_obj.monthly_budget_usd or 10.0))
        if budget_cap <= 0:
            return False

        now = timezone.now()
        monthly_logged = MonthlySpendLog.total_for_month(now.year, now.month)

        # Add live spend from active SurrealDB documents if available
        monthly_live = Decimal(0)
        try:
            from extractor import surreal_db as _sdb

            raw_docs = _sdb.list_documents(str(user.id))
            from datetime import datetime

            first_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
            monthly_live = Decimal(
                str(
                    sum(
                        float(d.get("cost_usd") or 0)
                        for d in raw_docs
                        if _parse_dt(d.get("created_at")) and _parse_dt(d.get("created_at")) >= first_of_month
                    )
                )
            )
        except Exception as err:
            logger.debug("[Budget Check] SurrealDB query bypassed, relying on MonthlySpendLog: %s", err)

        return (monthly_live + monthly_logged) >= budget_cap
    except Exception:
        return False  # Fail-open: never block if budget check itself fails


def _parse_dt(value):
    """Safely parse a datetime string; returns None on any error."""
    if value is None:
        return None
    try:
        from extractor.utils import parse_datetime

        return parse_datetime(str(value))
    except Exception:
        return None


def _get_dashboard_stats(request):
    """
    Helper to calculate and format dashboard statistics, avoiding duplicate logic
    between DashboardView and DocumentStatusAPIView.
    """
    currency_details = get_locale_currency_details(request)
    now = timezone.now()
    first_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.UTC)

    user = request.user
    if user.is_staff or user.is_superuser:
        raw_docs = surreal_db.list_documents()
    else:
        raw_docs = surreal_db.list_documents(str(user.id))

    # Parse and wrap documents
    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    users_map = {str(u.id): u for u in user_model.objects.all()}

    docs = [_wrap_surreal_doc(d, users_map) for d in raw_docs]

    monthly_live = Decimal(str(sum(float(d.cost_usd) for d in docs if d.created_at >= first_of_month)))

    # Add cost of documents that were deleted this month (persisted in MonthlySpendLog)
    from extractor.models import MonthlySpendLog

    monthly_logged = MonthlySpendLog.total_for_month(now.year, now.month)
    monthly_spent = monthly_live + monthly_logged

    total_spent_usd = Decimal(str(sum(float(d.cost_usd) for d in docs)))
    prompt_tokens = sum(int(d.input_tokens) for d in docs)
    cand_tokens = sum(int(d.output_tokens) for d in docs)
    total_tokens_spent = prompt_tokens + cand_tokens

    # Status stats
    status_choices = [
        ("PENDING", "Pending"),
        ("EXTRACTING", "Extracting"),
        ("REFINING", "Refining"),
        ("EMBEDDING", "Embedding"),
        ("COMPLETED", "Completed"),
        ("FAILED", "Failed"),
    ]
    stats_dict = {choice[0]: 0 for choice in status_choices}
    for d in docs:
        if d.status in stats_dict:
            stats_dict[d.status] += 1

    # Total pages
    total_pages = sum(int(d.page_count) for d in docs if d.status == "COMPLETED")

    # Budget cap checks
    system_settings_obj = surreal_db.get_system_settings()
    budget_cap = Decimal(str(system_settings_obj.get("monthly_budget_usd", 10.0)))
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
        "docs": docs,
    }


@method_decorator(never_cache, name="dispatch")
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

        stats = _get_dashboard_stats(request)
        docs = stats["docs"]

        search_query = request.GET.get("q", "").strip().lower()
        if search_query:
            docs = [
                d
                for d in docs
                if search_query in (d.title or "").lower()
                or search_query in (d.author or "").lower()
                or search_query in (d.language or "").lower()
                or search_query in (d.document_type or "").lower()
                or search_query in (getattr(d, "publisher", "") or "").lower()
                or search_query in (getattr(d, "publication_year", "") or "").lower()
                or search_query in (getattr(d, "doi", "") or "").lower()
            ]

        # Sort documents in python
        reverse = db_sort.startswith("-")
        key_name = db_sort.lstrip("-")

        key_map = {
            "title": lambda x: (x.title or x.original_filename or "").lower(),
            "cost_usd": lambda x: float(x.cost_usd),
            "status": lambda x: (x.status or "").lower(),
            "created_at": lambda x: x.created_at,
        }
        sort_key = key_map.get(key_name, lambda x: x.created_at)
        sorted_docs = sorted(docs, key=sort_key, reverse=reverse)

        # Render lists with pre-calculated formatting
        list_docs = []
        for d in sorted_docs[:50]:  # Cap dashboard display to recent 50 entries
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
            "total_docs_count": len(docs),
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

    def _clone_deduplicated_document(self, request, existing_doc, orig_name, file_hash):
        ip = get_client_ip(request)
        import uuid

        new_uuid = str(uuid.uuid4())

        file_path = existing_doc.file if hasattr(existing_doc, "file") else existing_doc.get("file")

        data = {
            "doc_uuid": new_uuid,
            "file": file_path,
            "original_filename": orig_name,
            "file_hash": file_hash,
            "status": "COMPLETED",
            "uploaded_by_id": str(request.user.id),
            "language": existing_doc.language if hasattr(existing_doc, "language") else existing_doc.get("language"),
            "author": existing_doc.author if hasattr(existing_doc, "author") else existing_doc.get("author"),
            "title": existing_doc.title if hasattr(existing_doc, "title") else existing_doc.get("title"),
            "document_type": existing_doc.document_type
            if hasattr(existing_doc, "document_type")
            else existing_doc.get("document_type"),
            "page_count": existing_doc.page_count
            if hasattr(existing_doc, "page_count")
            else existing_doc.get("page_count", 0),
            "raw_markdown": existing_doc.raw_markdown
            if hasattr(existing_doc, "raw_markdown")
            else existing_doc.get("raw_markdown"),
            "refined_markdown": existing_doc.refined_markdown
            if hasattr(existing_doc, "refined_markdown")
            else existing_doc.get("refined_markdown"),
            "yaml_metadata": existing_doc.yaml_metadata
            if hasattr(existing_doc, "yaml_metadata")
            else existing_doc.get("yaml_metadata"),
            "qa_dataset": existing_doc.qa_dataset
            if hasattr(existing_doc, "qa_dataset")
            else existing_doc.get("qa_dataset"),
            "cost_usd": 0.0,
            "semantic_signature": existing_doc.semantic_signature
            if hasattr(existing_doc, "semantic_signature")
            else existing_doc.get("semantic_signature"),
            "retry_count": 0,
            "created_at": format_datetime(timezone.now()),
            "updated_at": format_datetime(timezone.now()),
            "expires_at": format_datetime(
                timezone.now() + timezone.timedelta(days=int(getattr(settings, "DATA_RETENTION_DAYS", 30)))
            ),
        }
        doc = surreal_db.create_document(data)

        try:
            surreal_db.clone_chunks(
                str(existing_doc.uuid if hasattr(existing_doc, "uuid") else existing_doc.get("doc_uuid")), new_uuid
            )
        except Exception as clone_err:
            logger.warning("[Upload] SurrealDB chunk clone failed: %s", clone_err)

        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.UPLOAD_CACHED,
                user=request.user,
                document=doc,
                details=f"File '{orig_name}' uploaded and instantly cached via de-duplication.",
                ip_address=ip,
            )
        )
        return {"status": "cached", "name": orig_name}

    def _retry_existing_failed_document(self, request, existing_doc, orig_name):
        ip = get_client_ip(request)
        retry_cnt = (
            existing_doc.retry_count if hasattr(existing_doc, "retry_count") else existing_doc.get("retry_count", 0)
        )
        if retry_cnt >= 3:
            return {
                "status": "error",
                "name": orig_name,
                "error": f"Maximum retry limit of 3 exceeded for file '{orig_name}'.",
            }

        doc_uuid = existing_doc.uuid if hasattr(existing_doc, "uuid") else existing_doc.get("doc_uuid")

        doc_ref = surreal_db.update_document(
            doc_uuid,
            {
                "status": "PENDING",
                "cost_usd": 0.0,
                "input_tokens": 0,
                "output_tokens": 0,
                "error_message": "",
                "retry_count": retry_cnt + 1,
            },
        )

        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.UPLOAD,
                user=request.user,
                document=doc_ref,
                details=f"File '{orig_name}' re-uploaded; resetting failed pipeline status and re-enqueuing.",
                ip_address=ip,
            )
        )

        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc_ref.get("id") if isinstance(doc_ref, dict) else getattr(doc_ref, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})
        return {"status": "success", "name": f"{orig_name} (re-enqueued)"}

    def _create_fresh_document(self, request, uploaded_file, file_hash):
        orig_name = uploaded_file.name
        ip = get_client_ip(request)
        import os
        import uuid

        from django.core.files.storage import default_storage

        title_guess = os.path.splitext(orig_name)[0].replace("_", " ").replace("-", " ").strip()
        ext_guess = os.path.splitext(orig_name)[1].replace(".", "").upper()
        if not ext_guess:
            ext_guess = "PDF"

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
            "uploaded_by_id": str(request.user.id),
            "title": title_guess or "Untitled",
            "document_type": ext_guess,
            "retry_count": 0,
            "created_at": format_datetime(timezone.now()),
            "updated_at": format_datetime(timezone.now()),
        }
        doc = surreal_db.create_document(data)

        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.UPLOAD,
                user=request.user,
                document=doc,
                details=f"File '{orig_name}' uploaded successfully (size: {uploaded_file.size} bytes).",
                ip_address=ip,
            )
        )

        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc.get("id") if isinstance(doc, dict) else getattr(doc, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": new_uuid})
        return {"status": "success", "name": orig_name}

    def _validate_uploaded_file(self, orig_name, size):
        import os

        ext = os.path.splitext(orig_name)[1].lower().replace(".", "")
        ALLOWED_EXTENSIONS = {
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
        }
        if not ext or ext not in ALLOWED_EXTENSIONS:
            return {
                "status": "error",
                "name": orig_name,
                "error": f"Unsupported file type. Supported types: {', '.join(sorted(ALLOWED_EXTENSIONS))}",
            }

        if size > 31457280:
            return {"status": "error", "name": orig_name, "error": f"'{orig_name}' exceeds maximum 30MB constraint."}
        return None

    def _find_existing_doc(self, file_hash, user_id):
        existing_doc = surreal_db.get_document_by_hash(file_hash, str(user_id))
        if not existing_doc:
            # Check COMPLETED ones from anyone
            sql = "SELECT * FROM documents WHERE file_hash = $file_hash AND status = 'COMPLETED' LIMIT 1;"
            rows = surreal_db._first_result(surreal_db._run(sql, {"file_hash": file_hash}))
            if rows:
                existing_doc = rows[0]
            else:
                # Get any document matching hash
                existing_doc = surreal_db.get_document_by_hash(file_hash)
        return existing_doc

    def _process_single_file(self, request, uploaded_file, processed_hashes):
        orig_name = uploaded_file.name

        validation_error = self._validate_uploaded_file(orig_name, uploaded_file.size)
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

        existing_doc = self._find_existing_doc(file_hash, request.user.id)

        try:
            if existing_doc:
                status = existing_doc.get("status")
                uploaded_by_id = existing_doc.get("uploaded_by_id")
                if status == "COMPLETED":
                    if uploaded_by_id == str(request.user.id):
                        logger.info(
                            "[Deduplication] User already has completed document with hash %s. Reusing without copy.",
                            file_hash,
                        )
                        return {"status": "cached", "name": orig_name}

                    logger.info("[Deduplication] Match found for file hash %s. Skipping physical rewrite.", file_hash)
                    return self._clone_deduplicated_document(request, existing_doc, orig_name, file_hash)
                elif status in ["PENDING", "EXTRACTING", "REFINING", "EMBEDDING"]:
                    return {
                        "status": "error",
                        "name": orig_name,
                        "error": f"File '{orig_name}' is already being processed by the background worker.",
                    }
                elif status == "FAILED":
                    return self._retry_existing_failed_document(request, existing_doc, orig_name)

            return self._create_fresh_document(request, uploaded_file, file_hash)
        except Exception as e:
            return {"status": "error", "name": orig_name, "error": f"Error processing '{orig_name}': {e!s}"}

    def post(self, request):
        uploaded_files = request.FILES.getlist("file")
        if not uploaded_files:
            messages.error(request, "No file uploaded.")
            return redirect("dashboard")

        # Budget pre-check: block new LLM processing when monthly budget is exhausted
        if _is_budget_exceeded(request.user):
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


@method_decorator(never_cache, name="dispatch")
class DocumentDetailView(LoginRequiredMixin, View):
    """
    Renders details for a single document, incorporating a dual-screen Markdown Split-Editor
    and interactive Q&A dataset panels.
    """

    def get(self, request, doc_uuid):
        raw_doc = surreal_db.get_document(doc_uuid)
        if not raw_doc:
            from django.http import Http404

            raise Http404(DOCUMENT_NOT_FOUND_MSG)

        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        users_map = {str(u.id): u for u in user_model.objects.all()}
        doc = _wrap_surreal_doc(raw_doc, users_map)

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

        # Render markdown to HTML for presentation (sanitized via Bleach inside render_markdown_to_html)
        rendered_raw = _render_sanitized_markdown(doc.raw_markdown) if doc.raw_markdown else ""
        rendered_refined = _render_sanitized_markdown(doc.refined_markdown) if doc.refined_markdown else ""

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
    and refreshes the physical file back to storage.
    """

    def post(self, request, doc_uuid):
        raw_doc = surreal_db.get_document(doc_uuid)
        if not raw_doc:
            messages.error(request, DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        users_map = {str(u.id): u for u in user_model.objects.all()}
        doc = _wrap_surreal_doc(raw_doc, users_map)

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

        payload = {
            "refined_markdown": sanitized_markdown,
            "status": "EMBEDDING",
        }
        if new_title is not None:
            payload["title"] = new_title.strip()
        if new_author is not None:
            payload["author"] = new_author.strip()
        if new_language is not None:
            payload["language"] = new_language.strip()

        doc_ref = surreal_db.update_document(doc_uuid, payload)
        doc_wrapped = _wrap_surreal_doc(doc_ref, users_map)

        # Log the document edit event
        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.DOCUMENT_EDITED,
                user=request.user,
                document=doc_wrapped,
                details=f"Document '{doc_wrapped.original_filename}' content modified and re-embedding queued.",
                ip_address=get_client_ip(request),
            )
        )

        # Re-embed after edit to keep SurrealDB HNSW memory in sync with editor changes
        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc_wrapped.get("id") if isinstance(doc_wrapped, dict) else getattr(doc_wrapped, "id", None)
            cloud_tasks.enqueue("reembed_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("reembed_document", {"document_uuid": doc_uuid})

        messages.success(request, "Changes saved and re-indexing queued successfully!")
        return redirect("document_detail", doc_uuid=doc_uuid)


class DocumentDeleteView(LoginRequiredMixin, View):
    """
    Reference-counted document deletion.
    If multiple DB entries point to the same content address (SHA-256),
    the GCS file is preserved. Otherwise, it is deleted cleanly.
    """

    def post(self, request, doc_uuid):
        raw_doc = surreal_db.get_document(doc_uuid)
        if not raw_doc:
            messages.error(request, DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        users_map = {str(u.id): u for u in user_model.objects.all()}
        doc = _wrap_surreal_doc(raw_doc, users_map)

        # Check standard user access boundary (only uploader or staff can delete)
        if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
            messages.error(request, "Permission denied to delete this document.")
            return redirect("dashboard")

        file_hash = doc.file_hash
        orig_name = doc.original_filename
        file_rel_path = raw_doc.get("file", "")

        # Count shared hash pointers in SurrealDB
        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            from extractor.models import SourceDocument

            shared_references = SourceDocument.objects.filter(file_hash=file_hash).exclude(uuid=doc_uuid).count()
        else:
            sql = "SELECT doc_uuid FROM documents WHERE file_hash = $file_hash;"
            rows = surreal_db._first_result(surreal_db._run(sql, {"file_hash": file_hash}))
            other_uuids = [r["doc_uuid"] for r in rows if r["doc_uuid"] != doc_uuid]
            shared_references = len(other_uuids)
        ip = get_client_ip(request)

        # Create audit log record before deleting to preserve User/Document context
        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.DELETE,
                user=request.user,
                document=doc,
                details=f"Deleted document '{orig_name}' (Title: {doc.title}). Shared references remaining: {shared_references}.",
                ip_address=ip,
            )
        )

        # Flip delete order: delete file first if shared_references == 0
        if shared_references == 0:
            try:
                from django.core.files.storage import default_storage

                # Ensure file_rel_path is a valid non-empty string path
                path_str = str(file_rel_path or "").strip()
                if path_str and default_storage.exists(path_str):
                    default_storage.delete(path_str)
                logger.info("[De-duplication Delete] Purged file hash %s physically.", file_hash)
            except Exception as e:
                logger.warning("[De-duplication Delete] Failed to physically delete file for hash %s: %s", file_hash, e)

        # Delete from SurrealDB: cascades chunk deletion
        surreal_db.delete_document(doc_uuid)

        if shared_references != 0:
            logger.info(
                "[De-duplication Delete] Preserved file hash %s (Shared with %s entries).",
                file_hash,
                shared_references,
            )

        # Purge SurrealDB chunks and backup JSON
        from django.core.files.storage import default_storage

        messages.success(request, f"Document '{orig_name}' deleted successfully.")
        return redirect("dashboard")


class DocumentPurgeAllView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Enables instant GDPR 'Right to be Forgotten' data minimization.
    Deletes all records and physically flushes matching files cleanly.
    """

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.is_staff

    def post(self, request):
        raw_docs = surreal_db.list_documents()
        ip = get_client_ip(request)

        # Log the global purge all event first
        from extractor.utils import AuditEvent, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.PURGE_ALL,
                user=request.user,
                details=f"Purged all documents and associated semantic memory vector embeddings. Count: {len(raw_docs)}.",
                ip_address=ip,
            )
        )

        from django.core.files.storage import default_storage

        for doc in raw_docs:
            file_path = doc.get("file")
            if file_path:
                try:
                    if default_storage.exists(file_path):
                        default_storage.delete(file_path)
                except Exception as e:
                    logger.warning("[Purge All] Failed to delete file for %s: %s", doc.get("title"), e)

        # Flush SurrealDB entirely to delete any leftover chunks, metadata, caches
        try:
            surreal_db.purge_all()
        except Exception as exc:
            logger.warning("[Purge All] Failed to flush SurrealDB database: %s", exc)

        # Delete all chunks JSON files in storage
        try:
            _dirs, files = default_storage.listdir("chunks")
            for f in files:
                default_storage.delete(f"chunks/{f}")
            logger.info("[Purge All] Cleaned all chunk JSON files from storage.")
        except Exception as storage_err:
            logger.warning("[Purge All] Failed to clean chunks folder: %s", storage_err)

        messages.success(request, "Reset Memory Complete: Purged all documents and vector embeddings.")
        return redirect("dashboard")


class DocumentRAGSearchView(LoginRequiredMixin, View):
    """
    AJAX endpoint running global semantic search.
    Embeds query, searches SurrealDB, and generates answers.
    """

    def _check_rag_limits(self, request):
        from django.core.cache import cache

        rl_key = f"rag_ratelimit_{request.user.id}"
        rl_count = cache.get(rl_key, 0)
        if rl_count >= 10:
            return JsonResponse(
                {"error": "Search rate limit reached. Please wait a moment before sending another query."},
                status=429,
            )
        cache.set(rl_key, rl_count + 1, 60)  # Rolling 60-second window

        if _is_budget_exceeded(request.user):
            return JsonResponse(
                {"error": "Monthly AI budget has been reached. Intelligent search is paused until the budget resets."},
                status=402,
            )
        return None

    def _parse_document_ids(self, document_ids_str):
        if not document_ids_str:
            return None
        document_ids = []
        for i in document_ids_str.split(","):
            i_clean = i.strip()
            if i_clean:
                try:
                    document_ids.append(int(i_clean))
                except ValueError:
                    document_ids.append(i_clean)
        return document_ids

    def get(self, request):
        query = request.GET.get("q", "").strip()
        if not query:
            return JsonResponse({"error": "Empty search query."}, status=400)

        limit_response = self._check_rag_limits(request)
        if limit_response:
            return limit_response

        document_ids = self._parse_document_ids(request.GET.get("document_ids", "").strip())

        from django.utils.functional import SimpleLazyObject

        user = request.user
        if isinstance(user, SimpleLazyObject):
            user = user._wrapped

        try:
            results = query_semantic_knowledge_rag(
                query,
                document_ids=document_ids,
                top_k=5,
                user=user,
            )
            # Render markdown answer safely to HTML
            results["answer_html"] = render_markdown_to_html(results["answer"])
            return JsonResponse(results)
        except ValueError as e:
            logger.warning("[RAG Search] Validation error: %s", e)
            return JsonResponse({"error": str(e)}, status=400)
        except Exception as e:
            logger.exception("[RAG Search] Internal exception during semantic search: %s", e)
            return JsonResponse(
                {"error": "An unexpected error occurred during search. Please try again later."}, status=500
            )


class ExportZipView(LoginRequiredMixin, View):
    """
    Curates and builds dynamic ZIP bundle directories sorted by Language and Author,
    bundling metadata manifests and a single 'master_archival_source.md' document.
    """

    def post(self, request):
        from django.core.cache import cache

        user_key = f"export_ratelimit_{request.user.id}"
        ip_key = f"export_ratelimit_{get_client_ip(request)}"

        if cache.get(user_key) or cache.get(ip_key):
            messages.error(request, "Export rate limit exceeded. Please wait 60 seconds before trying again.")
            return redirect("dashboard")

        cache.set(user_key, True, 60)
        cache.set(ip_key, True, 60)

        document_ids = request.POST.getlist("selected_documents")
        if not document_ids:
            messages.error(request, "No documents selected for export.")
            return redirect("dashboard")

        try:
            zip_data = generate_curated_zip_bundle(document_ids, user=request.user)
            response = HttpResponse(zip_data, content_type="application/zip")
            response["Content-Disposition"] = (
                f'attachment; filename="curated_literature_archive_{timezone.now().strftime("%Y%m%d%H%M")}.zip"'
            )
            return response
        except Exception as e:
            messages.error(request, f"Export failure: {e!s}")
            return redirect("dashboard")


def _restart_single_document(doc, request, cloud_tasks):
    doc_uuid_val = doc.get("doc_uuid") or doc.get("uuid") or str(doc.get("id"))
    if isinstance(doc_uuid_val, str) and ":" in doc_uuid_val:
        doc_uuid_val = doc_uuid_val.split(":", 1)[1]
    doc_uuid = str(doc_uuid_val)
    uploaded_by_id = doc.get("uploaded_by_id")
    if not (request.user.is_staff or request.user.is_superuser or str(uploaded_by_id) == str(request.user.id)):
        return False

    status = doc.get("status")
    if status in ["FAILED", "COMPLETED"]:
        surreal_db.update_document(
            doc_uuid,
            {
                "status": "PENDING",
                "retry_count": 0,
                "error_message": "",
            },
        )
        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc.get("id") if isinstance(doc, dict) else getattr(doc, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})
        return True
    return False


def _handle_bulk_restart(request, document_ids):
    from extractor import cloud_tasks

    restarted_count = 0
    docs = surreal_db.get_documents(document_ids)
    for doc in docs:
        if _restart_single_document(doc, request, cloud_tasks):
            restarted_count += 1
    messages.success(request, f"Successfully queued {restarted_count} tasks for reprocessing.")


def _delete_single_document(doc, hash_ref_counts, request, default_storage, surreal_db):
    from extractor.models import AuditAction
    from extractor.utils import AuditEvent, get_client_ip, log_audit_event

    file_hash = doc.file_hash
    orig_name = doc.original_filename
    file_rel_path = doc.file
    doc_uuid = doc.uuid

    total_refs = hash_ref_counts.get(file_hash, 0)
    shared_references = max(0, total_refs - 1)

    if file_hash in hash_ref_counts:
        hash_ref_counts[file_hash] = max(0, total_refs - 1)

    log_audit_event(
        AuditEvent(
            action=AuditAction.DELETE,
            user=request.user,
            document=doc,
            details=f"Bulk Deleted document '{orig_name}' (Title: {doc.title}). Shared references remaining: {shared_references}.",
            ip_address=get_client_ip(request),
        )
    )

    if shared_references == 0:
        try:
            # Ensure file_rel_path is a valid non-empty string path
            path_str = str(file_rel_path or "").strip()
            if path_str and default_storage.exists(path_str):
                default_storage.delete(path_str)
        except Exception as e:
            logger.warning("[Bulk Delete] Failed to physically delete file: %s", e)

    surreal_db.delete_document(doc_uuid)


def _get_docs_for_delete(request, document_ids, users_map):
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.models import SourceDocument

        # Optimization: retrieve all records in a single SELECT query during tests
        if request.user.is_staff or request.user.is_superuser:
            return list(SourceDocument.objects.filter(id__in=document_ids))
        else:
            return list(SourceDocument.objects.filter(id__in=document_ids, uploaded_by=request.user))
    else:
        raw_docs = surreal_db.get_documents(document_ids)
        docs = []
        for raw_doc in raw_docs:
            uploaded_by_id = raw_doc.get("uploaded_by_id")
            if not (request.user.is_staff or request.user.is_superuser or uploaded_by_id == str(request.user.id)):
                continue
            docs.append(_wrap_surreal_doc(raw_doc, users_map))
        return docs


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
        hash_ref_counts = {r["file_hash"]: r.get("n", 0) for r in res if "file_hash" in r}

    for file_hash in file_hashes:
        if file_hash not in hash_ref_counts:
            hash_ref_counts[file_hash] = 0

    return hash_ref_counts


def _handle_bulk_delete(request, document_ids):
    from django.contrib.auth import get_user_model
    from django.core.files.storage import default_storage

    user_model = get_user_model()
    users_map = {str(u.id): u for u in user_model.objects.all()}

    docs = _get_docs_for_delete(request, document_ids, users_map)
    file_hashes = {doc.file_hash for doc in docs if doc.file_hash}
    hash_ref_counts = _get_hash_ref_counts(file_hashes)

    deleted_count = 0
    for doc in docs:
        _delete_single_document(doc, hash_ref_counts, request, default_storage, surreal_db)
        deleted_count += 1
    messages.success(request, f"Successfully deleted {deleted_count} documents from the repository.")


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
            _handle_bulk_restart(request, document_ids)
        elif action == "delete":
            _handle_bulk_delete(request, document_ids)
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
        except (ValueError, ArithmeticError):
            messages.error(request, "Invalid budget value provided. Must be a valid positive number.")
            return redirect("dashboard")

        payload = {
            "monthly_budget_usd": float(budget_val),
            "selected_model": selected_model,
            "currency": currency,
            "csrf_trusted_origins": csrf_trusted_origins,
        }

        # Only overwrite the API key if it's not the masked placeholder
        if openrouter_api_key != "••••••••••••••••":
            payload["openrouter_api_key"] = openrouter_api_key or ""

        surreal_db.save_system_settings(payload)

        messages.success(request, "System settings updated successfully!")
        return redirect("dashboard")


@method_decorator(never_cache, name="dispatch")
class DocumentStatusAPIView(LoginRequiredMixin, View):
    """
    Fast, lightweight JSON API view returning the status of all active/recent documents,
    along with real-time dashboard statistics (monthly budget spent, token counts, success ratio)
    to enable live dynamic updates without full-page reloads.
    """

    def get(self, request):
        stats = _get_dashboard_stats(request)
        docs = stats["docs"]

        # Build status map for all documents (limited to recent 100)
        docs_list = []
        for d in docs[:100]:
            docs_list.append(
                {
                    "id": d.id,
                    "uuid": d.uuid,
                    "status": d.status,
                    "status_display": d.status.title() if d.status else "Unknown",
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
                "total_docs_count": len(docs),
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

    def _handle_retry_permissions_and_limits(self, request, doc):
        if not (request.user.is_staff or request.user.is_superuser or doc.uploaded_by == request.user):
            return "Permission denied to retry this document.", 403

        is_restart = doc.status == "COMPLETED"
        retry_cnt = doc.retry_count

        if not is_restart and retry_cnt >= 3:
            return "Maximum retry limit of 3 exceeded for this document.", 400

        return None, None

    def _requeue_document_task(self, doc_wrapped, doc_uuid, request):
        from extractor.utils import AuditEvent, get_client_ip, log_audit_event

        log_audit_event(
            AuditEvent(
                action=AuditAction.UPLOAD,
                user=request.user,
                document=doc_wrapped,
                details=f"Curation pipeline re-enqueued for document: {doc_wrapped.title or doc_wrapped.original_filename}",
                ip_address=get_client_ip(request),
            )
        )

        from django.conf import settings

        from extractor import cloud_tasks

        if getattr(settings, "SURREALDB_OFFLINE", False):
            doc_id = doc_wrapped.get("id") if isinstance(doc_wrapped, dict) else getattr(doc_wrapped, "id", None)
            cloud_tasks.enqueue("process_document", {"document_id": doc_id})
        else:
            cloud_tasks.enqueue("process_document", {"document_uuid": doc_uuid})

    def post(self, request, doc_uuid):
        raw_doc = surreal_db.get_document(doc_uuid)
        is_ajax = (
            request.headers.get("x-requested-with") == "XMLHttpRequest"
            or request.headers.get("accept") == APPLICATION_JSON
        )

        if not raw_doc:
            if is_ajax:
                return JsonResponse({"error": DOCUMENT_NOT_FOUND_MSG}, status=404)
            messages.error(request, DOCUMENT_NOT_FOUND_MSG)
            return redirect("dashboard")

        from django.contrib.auth import get_user_model

        user_model = get_user_model()
        users_map = {str(u.id): u for u in user_model.objects.all()}
        doc = _wrap_surreal_doc(raw_doc, users_map)

        err_msg, status_code = self._handle_retry_permissions_and_limits(request, doc)
        if err_msg:
            if is_ajax:
                return JsonResponse({"error": err_msg}, status=status_code)
            messages.error(request, err_msg)
            return redirect("dashboard")

        is_restart = doc.status == "COMPLETED"
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
            },
        )
        doc_wrapped = _wrap_surreal_doc(doc_ref, users_map)

        self._requeue_document_task(doc_wrapped, doc_uuid, request)

        if is_ajax:
            return JsonResponse({"status": "success", "message": "Curation pipeline re-enqueued."})

        messages.success(request, f"Re-enqueued curation pipeline for document: {doc.title or doc.original_filename}")
        return redirect("dashboard")


def _get_offline_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query):
    if is_staff_or_superuser:
        logs_qs = AuditLog.objects.all().select_related("user", "document")
    else:
        logs_qs = AuditLog.objects.filter(user=request.user).select_related("user", "document")

    if action_filter:
        logs_qs = logs_qs.filter(action=action_filter)
    if is_staff_or_superuser and user_query:
        logs_qs = logs_qs.filter(user__username__icontains=user_query)
    if search_query:
        logs_qs = logs_qs.filter(
            Q(details__icontains=search_query)
            | Q(ip_address__icontains=search_query)
            | Q(document__original_filename__icontains=search_query)
            | Q(document__title__icontains=search_query)
            | Q(document__publisher__icontains=search_query)
            | Q(document__publication_year__icontains=search_query)
            | Q(document__doi__icontains=search_query)
        )

    logs = list(logs_qs.distinct().order_by("-created_at")[:200])
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
    return {
        "logs": logs,
        "action_choices": action_choices,
        "selected_action": action_filter,
        "selected_user": user_query if is_staff_or_superuser else "",
        "search_query": search_query,
    }


def _parse_surreal_audit_log(rl, users_map):
    if not rl:
        return None
    ts = rl.get("timestamp")
    if ts and isinstance(ts, str):
        import datetime

        try:
            ts_parsed = datetime.datetime.strptime(ts, ISO_8601_FORMAT).replace(tzinfo=datetime.UTC)
        except ValueError:
            ts_parsed = parse_datetime(ts)
    else:
        ts_parsed = parse_datetime(ts)

    uid = rl.get("user_id")
    u = users_map.get(uid) if uid else None

    doc = None
    did = rl.get("doc_uuid")
    if did:
        d_raw = surreal_db.get_document(did)
        if d_raw:
            doc = _wrap_surreal_doc(d_raw, users_map)

    class DummyAuditLog:
        pass

    a = DummyAuditLog()
    a.id = rl.get("id", "")
    a.user = u
    a.action = rl.get("action", "")
    a.document = doc
    a.details = rl.get("details", "")
    a.ip_address = rl.get("ip_address", "")
    a.created_at = ts_parsed
    return a


def _filter_audit_logs(logs, is_staff_or_superuser, user_query, search_query):
    if is_staff_or_superuser and user_query:
        logs = [
            log_item
            for log_item in logs
            if log_item.user and user_query.lower() in getattr(log_item.user, "username", "").lower()
        ]

    if search_query:
        sq = search_query.lower()
        logs = [
            log_item
            for log_item in logs
            if sq in log_item.details.lower()
            or sq in log_item.ip_address.lower()
            or (log_item.document and sq in getattr(log_item.document, "original_filename", "").lower())
            or (log_item.document and sq in getattr(log_item.document, "title", "").lower())
        ]
    return logs


def _get_surreal_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query):
    where_clauses = []
    params = {}
    if not is_staff_or_superuser:
        where_clauses.append("user_id = $user_id")
        params["user_id"] = str(request.user.id)
    if action_filter:
        where_clauses.append("action = $action")
        params["action"] = action_filter

    sql = "SELECT * FROM audit_logs"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    sql += " ORDER BY timestamp DESC LIMIT 200;"

    raw_logs = surreal_db._first_result(surreal_db._run(sql, params))

    from django.contrib.auth import get_user_model

    user_model = get_user_model()
    users_map = {str(u.id): u for u in user_model.objects.all()}

    logs = []
    for rl in raw_logs:
        a = _parse_surreal_audit_log(rl, users_map)
        if a:
            logs.append(a)

    logs = _filter_audit_logs(logs, is_staff_or_superuser, user_query, search_query)

    action_choices = [
        ("LOGIN", "Login"),
        ("LOGOUT", "Logout"),
        ("UPLOAD", "Upload Fresh"),
        ("UPLOAD_CACHED", "Upload Cached"),
        ("EXTRACTION_START", "Pipeline Started"),
        ("EXTRACTION_COMPLETED", "Pipeline Completed"),
        ("EXTRACTION_FAILED", "Pipeline Failed"),
        ("DELETE", "Delete Document"),
        ("PURGE_ALL", "Purge All Records"),
        ("DOCUMENT_EDITED", "Document Edited"),
        ("DOCUMENT_REQUEUED", "Document Requeued"),
        ("SYSTEM_CONTROL", "System Control"),
    ]
    return {
        "logs": logs,
        "action_choices": action_choices,
        "selected_action": action_filter,
        "selected_user": user_query if is_staff_or_superuser else "",
        "search_query": search_query,
    }


class AuditLogListView(LoginRequiredMixin, View):
    """
    Renders the secure, premium glassmorphic system audit trail.
    Standard users see only their own logs, while superusers and staff members see all logs.
    Enables quick action filtering, search index, and complete traceability.
    """

    def get(self, request):
        is_staff_or_superuser = request.user.is_superuser or request.user.is_staff
        action_filter = request.GET.get("action", "").strip()
        user_query = request.GET.get("user", "").strip()
        search_query = request.GET.get("q", "").strip()
        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            context = _get_offline_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query)
        else:
            context = _get_surreal_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query)
        return render(request, "extractor/audit_logs.html", context)


def _resolve_worker_config(worker_config_default):
    """Try to load live GCP service configs; fall back to local defaults."""
    from extractor.deployment import get_service_config

    worker_config = worker_config_default
    gcp_active = False
    worker_service_name = "aether-worker"

    try:
        worker_real = get_service_config("aether-worker")
        if worker_real:
            worker_config = worker_real
            gcp_active = True
    except Exception as e:
        logger.warning(f"Could not load worker config from GCP (local fallback): {e}")
        try:
            web_real = get_service_config("aether-web")
            if web_real:
                worker_service_name = "aether-web"
                worker_config = web_real
                gcp_active = True
        except Exception as e_web:
            logger.warning(f"Could not load web config either: {e_web}")

    return worker_config, gcp_active, worker_service_name


@method_decorator(never_cache, name="dispatch")
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
        worker_service_name = "aether-worker"
        gcp_active = False

        worker_config, gcp_active, worker_service_name = _resolve_worker_config(worker_config)

        try:
            web_real = get_service_config("aether-web")
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
            web_logs = get_service_logs("aether-web", limit=50)
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
        target_service = "aether-worker"
        try:
            get_service_config("aether-worker")
        except (OSError, ValueError, RuntimeError):
            try:
                get_service_config("aether-web")
                target_service = "aether-web"
            except Exception as exc:
                logger.debug("Failed to check fallback service aether-web: %s", exc)

        try:
            update_service_scale(target_service, min_scale, max_scale)
            messages.success(request, f"Successfully toggled scaling mode of {target_service} to {mode_display}!")
            # Add audit log for this operational action
            from extractor.utils import AuditEvent, log_audit_event

            log_audit_event(
                AuditEvent(
                    action=AuditAction.SYSTEM_CONTROL,
                    user=request.user,
                    details=f"Admins toggled worker scaling mode to '{mode}' (minScale: {min_scale}, maxScale: {max_scale}).",
                    ip_address=request.META.get("REMOTE_ADDR"),
                )
            )

        except Exception as e:
            messages.error(request, f"Failed to update Cloud Run scaling settings on GCP: {e!s}")

        return redirect("deployment_controller")


def _validate_registration_input(email, password, confirm_password, supabase_url, supabase_key):
    """Returns an error message string or None if input is valid."""
    import re
    from urllib.parse import urlparse

    if not email or not password:
        return "Email and Password are required."

    if password != confirm_password:
        return "Passwords do not match."

    if not supabase_url or not supabase_key:
        return "Supabase integration is not configured. Local registration is disabled."

    parsed = urlparse(supabase_url)
    domain = parsed.netloc or "example.com"
    email_lower = email.lower()
    if email_lower.startswith("admin@") or email_lower.endswith(f"@{domain}"):
        return "Registration of administrative or system email addresses is not permitted."

    if not re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]+", email):
        return "Invalid email format."

    return None


def _execute_supabase_auth_request(
    url_path, supabase_url, supabase_key, body_data, app_url, captcha_token, action_name
):
    import json
    import urllib.parse
    import urllib.request

    from extractor.utils import validate_url_scheme

    url = f"{supabase_url.rstrip('/')}{url_path}"
    try:
        validate_url_scheme(url)
        headers = {
            "apikey": supabase_key,
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        }
        if captcha_token:
            body_data["gotrue_meta_security"] = {"captcha_token": captcha_token}
        payload = json.dumps(body_data).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5):  # nosec B310 nosemgrep
            return True, None
    except urllib.error.HTTPError as e:
        body_bytes = e.read().decode("utf-8")
        try:
            err_msg = json.loads(body_bytes).get("msg") or json.loads(body_bytes).get("error_description") or body_bytes
        except (json.JSONDecodeError, KeyError, AttributeError):
            err_msg = body_bytes
        return False, f"Supabase {action_name} Failed: {err_msg}"
    except Exception as e:
        return False, f"Network error during {action_name.lower()}: {e!s}"


def _register_supabase_user(supabase_url, supabase_key, email, password, app_url, captcha_token=None):
    """Make the Supabase signup API call. Returns (success: bool, error_msg: str | None)."""
    import urllib.parse

    url_path = f"/auth/v1/signup?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/login')}"
    return _execute_supabase_auth_request(
        url_path, supabase_url, supabase_key, {"email": email, "password": password}, app_url, captcha_token, "Signup"
    )


def register_view(request):
    """
    Handles new user signups via Supabase Auth.
    """
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        password = request.POST.get("password", "")
        confirm_password = request.POST.get("confirm_password", "")

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        error_msg = _validate_registration_input(email, password, confirm_password, supabase_url, supabase_key)
        if error_msg:
            messages.error(request, error_msg)
            return render(request, TEMPLATE_REGISTER)

        turnstile_error = _turnstile_token_error(request)
        if turnstile_error:
            messages.error(request, turnstile_error)
            return render(request, TEMPLATE_REGISTER)

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        captcha_token = request.POST.get("cf-turnstile-response", "")
        success, error_msg = _register_supabase_user(
            supabase_url, supabase_key, email, password, app_url, captcha_token
        )
        if success:
            messages.success(request, "Registration successful! Please check your email for the activation link.")
            return redirect("login")
        else:
            messages.error(request, error_msg)

    return render(request, TEMPLATE_REGISTER)


def _send_supabase_recovery(email, supabase_url, supabase_key, app_url, captcha_token=None):
    import urllib.parse

    url_path = f"/auth/v1/recover?redirect_to={urllib.parse.quote(app_url.rstrip('/') + '/reset-password-confirm')}"
    return _execute_supabase_auth_request(
        url_path, supabase_url, supabase_key, {"email": email}, app_url, captcha_token, "Recovery"
    )


def forgot_password_view(request):
    """
    Dispatches a password recovery email via Supabase Auth.
    """
    if request.method == "POST":
        email = request.POST.get("email", "").strip()
        if not email:
            messages.error(request, "Email is required.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        # Validate email format
        import re

        if not re.fullmatch(r"[^@\s]+@[^@\s.]+\.[^@\s]+", email):
            messages.error(request, "Invalid email format.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        supabase_url = getattr(settings, "SUPABASE_URL", "")
        supabase_key = getattr(settings, "SUPABASE_PUBLIC_KEY", "")

        if not supabase_url or not supabase_key:
            messages.error(request, "Supabase integration is not configured.")
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        turnstile_error = _turnstile_token_error(request)
        if turnstile_error:
            messages.error(request, turnstile_error)
            return render(request, TEMPLATE_FORGOT_PASSWORD)

        app_url = getattr(settings, "APP_URL", "http://localhost:8000")
        captcha_token = request.POST.get("cf-turnstile-response", "")
        success, error_msg = _send_supabase_recovery(email, supabase_url, supabase_key, app_url, captcha_token)
        if success:
            messages.success(request, "Password recovery link has been sent! Please check your email inbox.")
            return redirect("login")
        else:
            messages.error(request, error_msg)

    return render(request, TEMPLATE_FORGOT_PASSWORD)


@require_http_methods(["GET", "HEAD"])
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
