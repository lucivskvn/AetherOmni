import logging

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.shortcuts import redirect
from django.utils import timezone
from django.views import View

from extractor import view_common
from extractor.models import AuditAction
from extractor.utils import (
    HEADER_CACHE_PRIVATE_NO_TRANSFORM,
    HEADER_X_CONTENT_TYPE_OPTIONS,
    generate_curated_csv_bundle,
    generate_curated_sqlite_bundle,
    generate_curated_zip_bundle,
    generate_sft_dataset_pairs,
    generate_sft_jsonl_bundle,
    get_client_ip,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

EXPORT_RATE_LIMIT_MSG = "Export rate limit exceeded. Please wait 60 seconds before trying again."
EXPORT_FAILURE_MSG = "Export generation failed. Please try again later."
EXPORT_BUSY_MSG = "Another export is already in progress for your account. Please wait for it to finish."


def _acquire_export_lock(request) -> bool:
    """Single-flight guard: exports fully buffer output in memory, so
    concurrent exports multiply peak RSS several-fold (AUD-345)."""
    key = f"export_inflight_{view_common.get_request_actor_id(request)}"
    # TTL must exceed worst-case generation time for the 1000-doc/64MiB
    # budget, or the lock expires mid-export and a second export can start
    # (AUD-363).
    from django.conf import settings

    if not getattr(settings, "SURREALDB_OFFLINE", False):
        # LocMemCache is per-process: with multiple Gunicorn workers each has
        # its own lock table, so cache.add cannot exclude concurrent exports.
        # The SurrealDB kv_cache unique index gives a shared atomic check-and-set.
        from extractor import surreal_db as _sdb

        try:
            return _sdb.kv_cache_add(key, True, ttl_seconds=900)
        except Exception as exc:
            logger.warning("[ExportLock] Surreal lock backend error; allowing export: %s", exc)
            return True
    from django.core.cache import cache

    try:
        return bool(cache.add(key, True, 900))
    except Exception as exc:
        logger.warning("[ExportLock] Cache backend error; allowing export: %s", exc)
        return True


def _release_export_lock(request) -> None:
    from django.conf import settings

    key = f"export_inflight_{view_common.get_request_actor_id(request)}"
    try:
        if not getattr(settings, "SURREALDB_OFFLINE", False):
            from extractor import surreal_db as _sdb

            _sdb.kv_cache_delete(key)
            return
        from django.core.cache import cache

        cache.delete(key)
    except Exception as exc:
        logger.debug("[ExportLock] Failed to release export lock: %s", exc)


SFT_PREVIEW_RATE_LIMIT_MSG = "Preview rate limit reached. Please wait a moment before trying again."


def _reserve_export_rate_limit(request) -> bool:
    """Atomically reserves a 60-second export rate limit for both actor and IP keys.
    Returns True if reservation succeeded, or False if rate limit is active."""
    user_key = f"export_ratelimit_{view_common.get_request_actor_id(request)}"
    ip_key = f"export_ratelimit_{get_client_ip(request)}"

    if not getattr(settings, "DEBUG", False) and not getattr(settings, "SURREALDB_OFFLINE", False):
        return view_common._enforce_distributed_rate_limit(
            f"export:{user_key}", user_key, max_requests=1, window_seconds=60
        ) and view_common._enforce_distributed_rate_limit(f"export:{ip_key}", ip_key, max_requests=1, window_seconds=60)

    from django.core.cache import cache

    try:
        # cache.add sets key only if it does not exist (atomic in Redis/Memcached/LocMem)
        user_acquired = cache.add(user_key, True, 60)
        ip_acquired = cache.add(ip_key, True, 60)

        if not user_acquired or not ip_acquired:
            # If one key was newly acquired but the other was already locked,
            # clean up the newly set key so reservation fails cleanly without TTL reset
            if user_acquired:
                cache.delete(user_key)
            if ip_acquired:
                cache.delete(ip_key)
            return False
        return True
    except Exception as exc:
        logger.debug("[ExportRateLimit] Cache backend error, falling back to SurrealDB: %s", exc)
        try:
            from extractor import surreal_db

            # Enforce both actor and IP keys like the cache-backed path above;
            # checking only the actor key would leave per-IP export flooding
            # unthrottled during a cache outage.
            user_ok = surreal_db.check_rate_limit_atomic(f"export:{user_key}", max_requests=1, window_seconds=60)
            ip_ok = surreal_db.check_rate_limit_atomic(f"export:{ip_key}", max_requests=1, window_seconds=60)
            return user_ok and ip_ok
        except Exception as surreal_err:
            logger.warning("[ExportRateLimit] SurrealDB rate limit fallback error: %s", surreal_err)
            return False


class ExportZipView(LoginRequiredMixin, View):
    """
    Curates and builds dynamic ZIP bundle directories sorted by Language and Author,
    bundling metadata manifests and a single 'master_archival_source.md' document.
    """

    def post(self, request):

        document_ids = [d.strip() for d in request.POST.getlist("selected_documents") if d.strip()][:1000]
        if not document_ids:
            messages.error(request, "No documents selected for export.")
            return redirect("dashboard")

        # Acquire the single-flight lock before consuming a rate-limit slot:
        # a busy rejection must not burn the user's 1/min export quota (AUD-364).
        if not _acquire_export_lock(request):
            messages.error(request, EXPORT_BUSY_MSG)
            return redirect("dashboard")

        try:
            if not _reserve_export_rate_limit(request):
                messages.error(request, EXPORT_RATE_LIMIT_MSG)
                return redirect("dashboard")
            zip_data = generate_curated_zip_bundle(
                document_ids, user=request.user, actor_id=view_common.get_request_actor_id(request)
            )
            view_common._record_view_audit(
                request,
                AuditAction.EXPORT,
                f"Exported {len(document_ids)} documents as ZIP bundle.",
            )
            response = HttpResponse(zip_data, content_type="application/zip")
            response["Content-Disposition"] = (
                f'attachment; filename="curated_literature_archive_{timezone.now().strftime("%Y%m%d%H%M")}.zip"'
            )
            response[HEADER_X_CONTENT_TYPE_OPTIONS] = "nosniff"
            response["Cache-Control"] = HEADER_CACHE_PRIVATE_NO_TRANSFORM
            return response
        except Exception:
            logger.exception("[Export] ZIP bundle generation failed")
            messages.error(request, EXPORT_FAILURE_MSG)
            return redirect("dashboard")
        finally:
            _release_export_lock(request)


def _check_sft_preview_rate_limit(actor_id: str) -> JsonResponse | None:
    """Check SurrealDB (prod) or cache atomic sliding window for SFT preview endpoint."""
    rl_key = f"sft_preview_ratelimit_{actor_id}"
    if not view_common._enforce_distributed_rate_limit(f"sft:{rl_key}", rl_key, max_requests=10, window_seconds=60):
        logger.warning("[SFTPreviewRateLimit] SurrealDB rate limit fallback error or limit exceeded for %s", rl_key)
        return JsonResponse({"status": "error", "message": SFT_PREVIEW_RATE_LIMIT_MSG}, status=429)
    return None


class SFTDatasetPreviewView(LoginRequiredMixin, View):
    """Returns a JSON payload with SFT prompt-completion dataset preview pairs for a document."""

    def get(self, request, doc_uuid):
        doc_uuid_str = str(doc_uuid) if doc_uuid else ""
        if not doc_uuid_str or len(doc_uuid_str) > 64:
            return JsonResponse({"status": "error", "message": "Invalid document identifier."}, status=400)

        actor_id = view_common.get_request_actor_id(request)
        rl_res = _check_sft_preview_rate_limit(actor_id)
        if rl_res:
            return rl_res

        raw_doc, _doc, is_authorized, _ = view_common._get_authorized_wrapped_doc(request, doc_uuid_str)
        if not raw_doc:
            return JsonResponse({"status": "error", "message": view_common.DOCUMENT_NOT_FOUND_MSG}, status=404)
        if not is_authorized:
            return JsonResponse({"status": "error", "message": "Permission denied."}, status=403)

        try:
            pairs = generate_sft_dataset_pairs(
                [doc_uuid],
                user=request.user,
                actor_id=actor_id,
                limit=25,
            )
            return JsonResponse(
                {
                    "status": "success",
                    "count": len(pairs),
                    "pairs": pairs,
                }
            )
        except Exception as exc:
            logger.warning("[SFTPreview] Failed to generate dataset preview: %s", exc)
            return JsonResponse(
                {
                    "status": "error",
                    "message": "Failed to generate dataset preview.",
                },
                status=400,
            )


class ExportSftJsonlView(LoginRequiredMixin, View):
    """Exports structured instruction fine-tuning dataset in Hugging Face / OpenAI JSONL format."""

    def post(self, request):

        document_ids = [d.strip() for d in request.POST.getlist("selected_documents") if d.strip()][:1000]
        if not document_ids:
            messages.error(request, "No documents selected for SFT JSONL export.")
            return redirect("dashboard")

        # Acquire the single-flight lock before consuming a rate-limit slot:
        # a busy rejection must not burn the user's 1/min export quota (AUD-364).
        if not _acquire_export_lock(request):
            messages.error(request, EXPORT_BUSY_MSG)
            return redirect("dashboard")

        try:
            if not _reserve_export_rate_limit(request):
                messages.error(request, EXPORT_RATE_LIMIT_MSG)
                return redirect("dashboard")
            jsonl_data = generate_sft_jsonl_bundle(
                document_ids,
                user=request.user,
                actor_id=view_common.get_request_actor_id(request),
            )
            view_common._record_view_audit(
                request,
                AuditAction.EXPORT,
                f"Exported {len(document_ids)} documents as SFT JSONL dataset.",
            )
            response = HttpResponse(jsonl_data, content_type="application/x-jsonlines")
            filename = f"sft_dataset_{timezone.now().strftime('%Y%m%d%H%M')}.jsonl"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response[HEADER_X_CONTENT_TYPE_OPTIONS] = "nosniff"
            response["Cache-Control"] = HEADER_CACHE_PRIVATE_NO_TRANSFORM
            return response
        except Exception:
            logger.exception("[Export] SFT JSONL generation failed")
            messages.error(request, EXPORT_FAILURE_MSG)
            return redirect("dashboard")
        finally:
            _release_export_lock(request)


class ExportSqliteView(LoginRequiredMixin, View):
    """Exports standalone SQLite database with FTS5 search index for offline mobile/desktop integration."""

    def post(self, request):

        document_ids = [d.strip() for d in request.POST.getlist("selected_documents") if d.strip()][:1000]
        if not document_ids:
            messages.error(request, "No documents selected for SQLite database export.")
            return redirect("dashboard")

        # Acquire the single-flight lock before consuming a rate-limit slot:
        # a busy rejection must not burn the user's 1/min export quota (AUD-364).
        if not _acquire_export_lock(request):
            messages.error(request, EXPORT_BUSY_MSG)
            return redirect("dashboard")

        try:
            if not _reserve_export_rate_limit(request):
                messages.error(request, EXPORT_RATE_LIMIT_MSG)
                return redirect("dashboard")
            sqlite_data = generate_curated_sqlite_bundle(
                document_ids,
                user=request.user,
                actor_id=view_common.get_request_actor_id(request),
            )
            view_common._record_view_audit(
                request,
                AuditAction.EXPORT,
                f"Exported {len(document_ids)} documents as SQLite FTS5 database.",
            )
            response = HttpResponse(sqlite_data, content_type="application/x-sqlite3")
            filename = f"curated_knowledge_{timezone.now().strftime('%Y%m%d%H%M')}.db"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response[HEADER_X_CONTENT_TYPE_OPTIONS] = "nosniff"
            response["Cache-Control"] = HEADER_CACHE_PRIVATE_NO_TRANSFORM
            return response
        except Exception:
            logger.exception("[Export] SQLite generation failed")
            messages.error(request, EXPORT_FAILURE_MSG)
            return redirect("dashboard")
        finally:
            _release_export_lock(request)


class ExportCsvView(LoginRequiredMixin, View):
    """Exports structured tabular CSV summary of document metadata, copyright, and content excerpts."""

    def post(self, request):

        document_ids = [d.strip() for d in request.POST.getlist("selected_documents") if d.strip()][:1000]
        if not document_ids:
            messages.error(request, "No documents selected for CSV export.")
            return redirect("dashboard")

        # Acquire the single-flight lock before consuming a rate-limit slot:
        # a busy rejection must not burn the user's 1/min export quota (AUD-364).
        if not _acquire_export_lock(request):
            messages.error(request, EXPORT_BUSY_MSG)
            return redirect("dashboard")

        try:
            if not _reserve_export_rate_limit(request):
                messages.error(request, EXPORT_RATE_LIMIT_MSG)
                return redirect("dashboard")
            csv_data = generate_curated_csv_bundle(
                document_ids,
                user=request.user,
                actor_id=view_common.get_request_actor_id(request),
            )
            view_common._record_view_audit(
                request,
                AuditAction.EXPORT,
                f"Exported {len(document_ids)} documents as CSV summary.",
            )
            response = HttpResponse(csv_data, content_type="text/csv; charset=utf-8")
            filename = f"curated_metadata_{timezone.now().strftime('%Y%m%d%H%M')}.csv"
            response["Content-Disposition"] = f'attachment; filename="{filename}"'
            response[HEADER_X_CONTENT_TYPE_OPTIONS] = "nosniff"
            response["Cache-Control"] = HEADER_CACHE_PRIVATE_NO_TRANSFORM
            return response
        except Exception:
            logger.exception("[Export] CSV generation failed")
            messages.error(request, EXPORT_FAILURE_MSG)
            return redirect("dashboard")
        finally:
            _release_export_lock(request)
