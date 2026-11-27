import json
import logging
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlparse

from django.conf import settings
from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import redirect, render
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from extractor import surreal_db, view_common
from extractor.models import AuditAction, AuditLog
from extractor.utils import (
    KEY_CSRF_TRUSTED_ORIGINS,
    KNATIVE_MIN_SCALE,
    get_client_ip,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

AUDIT_ACTION_CHOICES = (
    (AuditAction.LOGIN, "Login"),
    (AuditAction.LOGOUT, "Logout"),
    (AuditAction.UPLOAD, "Upload fresh"),
    (AuditAction.UPLOAD_CACHED, "Upload cached"),
    (AuditAction.EXTRACTION_START, "Pipeline started"),
    (AuditAction.EXTRACTION_COMPLETED, "Pipeline completed"),
    (AuditAction.EXTRACTION_FAILED, "Pipeline failed"),
    (AuditAction.DELETE, "Delete document"),
    (AuditAction.PURGE_ALL, "Purge all records"),
    (AuditAction.DOCUMENT_EDITED, "Document edited"),
    (AuditAction.DOCUMENT_REQUEUED, "Document requeued"),
    (AuditAction.DOCUMENT_CANCELLED, "Document cancelled"),
    (AuditAction.SYSTEM_CONTROL, "System control"),
    (AuditAction.EXPORT, "Export dataset"),
)
AUDIT_ACTION_ALIASES = {
    "UPLOAD_FRESH": AuditAction.UPLOAD,
    "PIPELINE_STARTED": AuditAction.EXTRACTION_START,
    "PIPELINE_COMPLETED": AuditAction.EXTRACTION_COMPLETED,
    "PIPELINE_FAILED": AuditAction.EXTRACTION_FAILED,
    "DELETE_DOCUMENT": AuditAction.DELETE,
    "PURGE_ALL_RECORDS": AuditAction.PURGE_ALL,
}


def normalize_audit_action(action):
    """Return the canonical audit action for current and legacy ledger entries."""
    normalized = str(action or "").strip().upper().replace("-", "_").replace(" ", "_")
    return AUDIT_ACTION_ALIASES.get(normalized, normalized)


class SaveSettingsView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Saves the configurable Monthly Budget, Source Library URI, LLM Model, and Custom Public Domains dynamically in SystemSettings.
    """

    def test_func(self):
        return self.request.user.is_superuser

    @staticmethod
    def _allowed_models() -> set[str]:
        from extractor.llm_gateway import KNOWN_GEMINI_MODELS

        # Strict validation whitelist for SystemSettings selected_model to prevent unauthorized/expensive models
        return (
            {
                "auto",
                "openrouter/free",
                "openrouter/auto",
                "meta-llama/llama-3-8b-instruct:free",
                "google/gemma-2-9b-it:free",
                "qwen/qwen-2-7b-instruct:free",
            }
            | KNOWN_GEMINI_MODELS
            | {f"google/{m}" for m in KNOWN_GEMINI_MODELS}
        )

    @staticmethod
    def _invalid_csrf_origins(csrf_trusted_origins: str) -> list[str]:
        """Return origin entries that are not bare HTTPS origins."""
        raw_entries = [
            entry.strip() for line in csrf_trusted_origins.splitlines() for entry in line.split(",") if entry.strip()
        ]
        bad = []
        for origin in raw_entries:
            parsed = urlparse(origin)
            if (
                parsed.scheme != "https"
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or parsed.path
            ):
                bad.append(origin)
        return bad

    def post(self, request):
        monthly_budget_usd = request.POST.get("monthly_budget_usd", "10.00").strip()
        selected_model = request.POST.get("selected_model", "auto").strip()
        currency = request.POST.get("currency", "auto").strip()
        csrf_trusted_origins = request.POST.get(KEY_CSRF_TRUSTED_ORIGINS, "").strip()[:8192]

        if selected_model not in self._allowed_models():
            messages.error(request, "Invalid model selection.")
            return redirect("dashboard")

        if currency not in {"auto", "USD", "IDR", "SAR"}:
            messages.error(request, "Invalid currency selection.")
            return redirect("dashboard")

        try:
            budget_val = Decimal(monthly_budget_usd)
            if budget_val < 0:
                raise ValueError("Budget cannot be negative.")
            if budget_val > 100000.00:
                raise ValueError("Budget cannot exceed $100,000.00.")
        except ValueError, ArithmeticError:
            messages.error(request, "Invalid budget value provided. Must be a valid positive number up to $100,000.00.")
            return redirect("dashboard")

        # Require HTTPS origins so saved configuration cannot weaken CSRF transport security.
        if csrf_trusted_origins:
            bad_origins = self._invalid_csrf_origins(csrf_trusted_origins)
            if bad_origins:
                messages.error(
                    request,
                    f"Invalid trusted origin(s): {', '.join(bad_origins)}. Each entry must use HTTPS.",
                )
                return redirect("dashboard")

        payload = {
            "monthly_budget_usd": float(budget_val),
            "selected_model": selected_model,
            "currency": currency,
            KEY_CSRF_TRUSTED_ORIGINS: csrf_trusted_origins,
        }

        try:
            surreal_db.save_system_settings(payload)
        except Exception as exc:
            logger.warning("[SaveSettings] SurrealDB settings save failed: %s", exc)
            messages.error(request, "Settings could not be saved: the settings store is unavailable. Please retry.")
            return redirect("dashboard")

        try:
            from extractor.models import SystemSettings

            SystemSettings.objects.update_or_create(
                id=1,
                defaults={
                    "monthly_budget_usd": budget_val,
                    "selected_model": selected_model,
                    "currency": currency,
                    KEY_CSRF_TRUSTED_ORIGINS: csrf_trusted_origins,
                },
            )
        except Exception as orm_err:
            logger.debug("[SaveSettings] SQLite settings sync skipped: %s", orm_err)

        view_common._record_view_audit(
            request,
            AuditAction.SYSTEM_CONTROL,
            f"Updated system settings: Budget=${budget_val:.2f}, Model={selected_model}, Currency={currency}.",
        )

        messages.success(request, "System settings updated successfully!")
        return redirect("dashboard")


def _get_offline_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query):
    if is_staff_or_superuser:
        logs_qs = AuditLog.objects.all().select_related("user", "document")
    else:
        logs_qs = AuditLog.objects.filter(user=request.user).select_related("user", "document")

    if action_filter:
        # Audit records are immutable and older deployments may have used a
        # different case. Canonicalize the request and match case-insensitively.
        logs_qs = logs_qs.filter(action__iexact=action_filter)
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
    return {
        "logs": logs,
        "action_choices": AUDIT_ACTION_CHOICES,
        "selected_action": action_filter,
        "selected_user": user_query if is_staff_or_superuser else "",
        "search_query": search_query,
    }


def _parse_surreal_audit_details(raw_log):
    """Return human-readable details from current and historical audit rows."""
    details = raw_log.get("details", "")
    metadata = raw_log.get("metadata")
    if details or not metadata:
        return str(details)
    if isinstance(metadata, dict):
        return str(metadata.get("details", ""))
    if not isinstance(metadata, str):
        return ""
    try:
        decoded_metadata = json.loads(metadata)
    except json.JSONDecodeError:
        return metadata
    return str(decoded_metadata.get("details", "") if isinstance(decoded_metadata, dict) else metadata)


def _get_surreal_audit_document(raw_log, users_map, docs_map=None):
    document_id = raw_log.get("doc_uuid")
    if not document_id:
        return None
    if docs_map is not None:
        raw_document = docs_map.get(document_id)
    else:
        raw_document = surreal_db.get_document(document_id)
    if raw_document:
        return view_common._wrap_surreal_doc(raw_document, users_map)
    # If the document was deleted or purged, provide a fallback object so Target File displays gracefully
    fallback = SimpleNamespace()
    fallback.uuid = document_id
    fallback.status = "DELETED"
    fallback.get_status_display = lambda: "Deleted"
    fallback.original_filename = f"Deleted Document ({document_id[:8]})"
    return fallback


def _parse_surreal_audit_log(rl, users_map, docs_map=None):
    if not rl:
        return None
    ts = rl.get("timestamp")
    if ts and isinstance(ts, str):
        import datetime

        try:
            ts_parsed = datetime.datetime.strptime(ts, view_common.ISO_8601_FORMAT).replace(tzinfo=datetime.UTC)
        except ValueError:
            ts_parsed = view_common.parse_datetime(ts)
    else:
        ts_parsed = view_common.parse_datetime(ts)

    uid = rl.get("user_id")
    u = users_map.get(uid) if uid else None

    class DummyAuditLog:
        pass

    a = DummyAuditLog()
    a.id = rl.get("id", "")
    a.user = u
    a.action = normalize_audit_action(rl.get("action"))
    a.document = _get_surreal_audit_document(rl, users_map, docs_map=docs_map)
    a.details = _parse_surreal_audit_details(rl)
    a.ip_address = rl.get("ip_address", "")
    a.created_at = ts_parsed
    a.timestamp = ts_parsed
    return a


def _filter_audit_logs(logs, is_staff_or_superuser, action_filter, user_query, search_query):
    if action_filter:
        logs = [log_item for log_item in logs if normalize_audit_action(log_item.action) == action_filter]

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
            if sq in (log_item.details or "").lower()
            or sq in (log_item.ip_address or "").lower()
            or (log_item.document and sq in getattr(log_item.document, "original_filename", "").lower())
            or (log_item.document and sq in getattr(log_item.document, "title", "").lower())
            or (log_item.document and sq in getattr(log_item.document, "publisher", "").lower())
            or (log_item.document and sq in getattr(log_item.document, "publication_year", "").lower())
            or (log_item.document and sq in getattr(log_item.document, "doi", "").lower())
        ]
    return logs


def _build_audit_docs_map(raw_logs):
    """Retrieve and map documents for the given audit logs in a single batch query."""
    doc_uuids = list({rl.get("doc_uuid") for rl in raw_logs if rl.get("doc_uuid")})
    if not doc_uuids:
        return {}

    raw_docs = surreal_db.get_documents(doc_uuids)
    docs_map = {}
    for d in raw_docs:
        if not d:
            continue
        doc_uuid = d.get("doc_uuid")
        if doc_uuid:
            docs_map[doc_uuid] = d
            docs_map[str(doc_uuid)] = d
        doc_id = d.get("id")
        if doc_id:
            docs_map[doc_id] = d
            docs_map[str(doc_id)] = d
    return docs_map


def _get_surreal_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query):
    where_clauses = []
    params = {}
    if not is_staff_or_superuser:
        where_clauses.append("user_id = $user_id")
        params["user_id"] = view_common.get_request_actor_id(request)
    sql = "SELECT * FROM audit_logs"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    # Filter actions after normalizing legacy ledger values. This keeps old,
    # immutable entries discoverable while all new writes remain canonical.
    # Action/user/search predicates are applied in Python after the fetch, so
    # widen the window when filters are active — otherwise matches beyond the
    # newest rows are silently invisible (AUD-313).
    row_limit = 5000 if (action_filter or user_query or search_query) else 500
    sql += f" ORDER BY timestamp DESC LIMIT {row_limit};"

    raw_logs = surreal_db._first_result(surreal_db._run(sql, params))

    user_ids = {rl.get("user_id") for rl in raw_logs if rl.get("user_id")}
    users_map = view_common._build_users_map(user_ids, fallback_user=request.user)
    actor_id = view_common.get_request_actor_id(request)
    if actor_id:
        users_map[actor_id] = request.user

    docs_map = _build_audit_docs_map(raw_logs)

    logs = []
    for rl in raw_logs:
        a = _parse_surreal_audit_log(rl, users_map, docs_map=docs_map)
        if a:
            logs.append(a)

    logs = _filter_audit_logs(logs, is_staff_or_superuser, action_filter, user_query, search_query)
    return {
        "logs": logs,
        "action_choices": AUDIT_ACTION_CHOICES,
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
        action_filter = normalize_audit_action(request.GET.get("action", ""))
        if action_filter and action_filter not in dict(AUDIT_ACTION_CHOICES):
            action_filter = ""
        user_query = request.GET.get("user", "").strip()
        search_query = request.GET.get("q", "").strip()
        try:
            if getattr(settings, "SURREALDB_OFFLINE", False):
                context = _get_offline_audit_logs(
                    request, is_staff_or_superuser, action_filter, user_query, search_query
                )
            else:
                context = _get_surreal_audit_logs(
                    request, is_staff_or_superuser, action_filter, user_query, search_query
                )
        except Exception as exc:
            logger.warning("[Audit Logs] Failed to retrieve SurrealDB logs, falling back to local store: %s", exc)
            # Surface the degraded store: in production the Django AuditLog
            # mirror can be empty/stale, and "no logs" would look like a clean
            # trail rather than a backend failure (AUD-314).
            messages.warning(
                request,
                "The primary audit store is unavailable; showing the local mirror, which may be incomplete.",
            )
            context = _get_offline_audit_logs(request, is_staff_or_superuser, action_filter, user_query, search_query)

        return render(request, "extractor/audit_logs.html", context)


_GCP_CONSOLE_CACHE: dict[str, tuple[float, Any]] = {}
_GCP_CONSOLE_CACHE_TTL = 60.0


def _cached_gcp_console_call(name: str, fn, *args, **kwargs):
    """Cache GCP console reads for ~60s so page loads don't burn API quota (AUD-312)."""
    import time

    now = time.time()
    hit = _GCP_CONSOLE_CACHE.get(name)
    if hit and now - hit[0] < _GCP_CONSOLE_CACHE_TTL:
        return hit[1]
    result = fn(*args, **kwargs)
    _GCP_CONSOLE_CACHE[name] = (now, result)
    return result


def _resolve_worker_config(worker_config_default):
    """Try to load live GCP service configs; fall back to local defaults."""
    from django.conf import settings

    from extractor.deployment import get_service_config

    worker_config = worker_config_default
    gcp_active = False
    worker_service_name = settings.WORKER_SERVICE_NAME

    try:
        worker_real = _cached_gcp_console_call(
            f"svc_cfg:{worker_service_name}", get_service_config, worker_service_name
        )
        if worker_real:
            worker_config = worker_real
            gcp_active = True
    except Exception as e:
        logger.warning(f"Could not load worker config from GCP (local fallback): {e}")

    return worker_config, gcp_active, worker_service_name


@method_decorator(never_cache, name="dispatch")
class DeploymentControllerView(LoginRequiredMixin, UserPassesTestMixin, View):
    """
    Centralized Deployment and Cost Console allowing admins/staff to:
    - View active Cloud Run configurations and scaling limits.
    - Superusers may change worker service mode ("Hibernating" min_instances=0, max_instances=1,
      "On-Demand" min_instances=0, max_instances=5, "Always-On" min_instances=1, max_instances=5).
    - Trigger QA system diagnostics.
    - View live aggregated container logs.
    """

    def test_func(self):
        return self.request.user.is_superuser or self.request.user.is_staff

    def handle_no_permission(self):
        if self.request.user.is_authenticated:
            messages.error(self.request, "Access restricted to system administrators.")
            return redirect("dashboard")
        return super().handle_no_permission()

    def get(self, request):
        from django.conf import settings

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
                            "autoscaling.knative.dev/maxScale": "5",
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
        worker_service_name = settings.WORKER_SERVICE_NAME
        gcp_active = False

        worker_config, gcp_active, worker_service_name = _resolve_worker_config(worker_config)

        try:
            web_real = _cached_gcp_console_call(
                f"svc_cfg:{settings.WEB_SERVICE_NAME}", get_service_config, settings.WEB_SERVICE_NAME
            )
            if web_real:
                web_config = web_real
        except Exception as e:
            logger.warning(f"Could not load web config from GCP (local fallback): {e}")

        # Extract scaling values (default worker fallback: min=0, max=5 for on-demand)
        worker_min, worker_max = extract_knative_scaling(worker_config, 0, 5)
        web_min, web_max = extract_knative_scaling(web_config, 1, 5)

        # Determine current mode
        if worker_min == 0 and worker_max <= 1:
            current_mode = "hibernate"
        elif worker_min == 0 and worker_max > 1:
            current_mode = "on-demand"
        else:
            current_mode = "always-on"

        # Fetch logs (last 50 rows). Container logs can contain document
        # content and request metadata — restrict them to superusers; staff
        # see the console without the log panels (AUD-312).
        can_view_logs = request.user.is_superuser
        if can_view_logs:
            try:
                worker_logs = _cached_gcp_console_call(
                    f"svc_logs:{worker_service_name}", get_service_logs, worker_service_name, limit=50
                )
            except Exception as e:
                worker_logs = [{"timestamp": "", "message": f"Log retrieval error: {e}", "severity": "ERROR"}]

            try:
                web_logs = _cached_gcp_console_call(
                    f"svc_logs:{settings.WEB_SERVICE_NAME}", get_service_logs, settings.WEB_SERVICE_NAME, limit=50
                )
            except Exception as e:
                web_logs = [{"timestamp": "", "message": f"Log retrieval error: {e}", "severity": "ERROR"}]

        # Check SurrealDB & downstream dependencies health status
        from extractor.deployment import check_service_dependencies_health

        health_probes = _cached_gcp_console_call("dep_health", check_service_dependencies_health)

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
            "can_view_logs": can_view_logs,
            "surreal_health": "ONLINE" if health_probes.get("surrealdb") else "OFFLINE",
            "supabase_health": "ONLINE" if health_probes.get("supabase") else "OFFLINE",
        }
        return render(request, "extractor/deployment_controller.html", context)

    def post(self, request):
        from django.conf import settings

        from extractor.deployment import (
            check_service_dependencies_health,
            get_service_config,
            update_service_scale,
        )

        if not request.user.is_superuser:
            return JsonResponse({"error": "Only administrators may change deployment scaling."}, status=403)

        mode = request.POST.get("mode", "").strip().lower()

        if mode == "hibernate":
            min_scale, max_scale = 0, 1
            mode_display = "Scale to zero when idle (requests can still start an instance)"
        elif mode == "on-demand":
            min_scale, max_scale = 0, 5
            mode_display = "On-Demand Serverless Scaling"
        elif mode == "always-on":
            min_scale, max_scale = 1, 5
            mode_display = "Always-On Performance Mode"
        else:
            messages.error(request, "Invalid deployment scaling mode selected.")
            return redirect("deployment_controller")

        # Preflight health check
        health_status = check_service_dependencies_health()
        if not health_status.get("surrealdb") and not getattr(settings, "SURREALDB_OFFLINE", False):
            messages.warning(request, "Preflight alert: SurrealDB is currently offline or unreachable.")

        # Resolve which service to scale on GCP
        target_service = settings.WORKER_SERVICE_NAME

        try:
            if not get_service_config(target_service):
                raise RuntimeError("Worker configuration is unavailable; scaling was not changed.")
            update_service_scale(target_service, min_scale, max_scale)
            messages.success(request, f"Successfully toggled scaling mode of {target_service} to {mode_display}!")
            view_common._record_view_audit(
                request,
                AuditAction.SYSTEM_CONTROL,
                f"Admins changed {target_service} to '{mode}' (minScale: {min_scale}, maxScale: {max_scale}).",
                ip=get_client_ip(request),
            )

        except Exception:
            logger.exception("[Deployment] Cloud Run scaling update failed")
            messages.error(request, "Cloud Run scaling update failed. Please try again later.")

        return redirect("deployment_controller")
