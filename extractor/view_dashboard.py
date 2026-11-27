import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse
from django.shortcuts import render
from django.utils import timezone
from django.utils.decorators import method_decorator
from django.views import View
from django.views.decorators.cache import never_cache

from extractor import surreal_db, view_common
from extractor.utils import (
    format_localized_cost,
    get_locale_currency_details,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

SNAPSHOT_CAPPED = "snapshot_capped"


def _get_unbounded_doc_aggregates(request, since: datetime | None = None) -> dict:
    """Spend/token/status aggregates over ALL scoped documents.

    The display snapshot is capped for page weight; stats derived from it
    undercount for heavy users and diverge from the worker budget gate
    (AUD-342). These aggregates query SurrealDB/Django directly instead.
    """
    from django.conf import settings

    user = request.user
    staff = bool(user.is_staff or user.is_superuser)
    actor_id = None if staff else view_common.get_request_actor_id(request)

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _doc_aggregates_offline(actor_id, since)
    return _doc_aggregates_surreal(actor_id, since)


def _doc_aggregates_offline(actor_id: str | None, since: datetime | None) -> dict:
    from django.db.models import Count, Q, Sum

    from extractor.models import SourceDocument

    qs = SourceDocument.objects.all()
    if actor_id:
        # Match list_document_statuses scoping: own docs plus unowned ones.
        qs = qs.filter(Q(uploaded_by_id=actor_id) | Q(uploaded_by__isnull=True))
    agg = qs.aggregate(
        total_cost=Sum("cost_usd"),
        total_in=Sum("input_tokens"),
        total_out=Sum("output_tokens"),
    )
    monthly_cost = qs.filter(updated_at__gte=since).aggregate(total=Sum("cost_usd"))["total"] if since else None
    pages = qs.filter(status="COMPLETED").aggregate(total=Sum("page_count"))["total"] or 0
    status_counts = {row["status"]: row["n"] for row in qs.values("status").annotate(n=Count("id"))}
    return {
        "total_cost": Decimal(str(agg["total_cost"] or 0)),
        "total_in": int(agg["total_in"] or 0),
        "total_out": int(agg["total_out"] or 0),
        "total_pages": int(pages),
        "status_counts": status_counts,
        "monthly_cost": Decimal(str(monthly_cost or 0)),
    }


def _doc_aggregates_surreal(actor_id: str | None, since: datetime | None) -> dict:
    # Match list_document_statuses scoping: own docs plus unowned ones.
    scope = " WHERE (uploaded_by_id = $uid OR uploaded_by_id = NONE)" if actor_id else ""
    params: dict[str, Any] = {"uid": actor_id} if actor_id else {}
    cost_sql = (
        "SELECT math::sum(cost_usd) AS total_cost, math::sum(input_tokens) AS tin, "  # noqa: S608 -- scope is a static clause; values are bound params
        f"math::sum(output_tokens) AS tout FROM documents{scope} GROUP ALL;"  # nosec B608
    )
    cost_rows = surreal_db._first_result(surreal_db._run(cost_sql, params))
    cost_row = cost_rows[0] if cost_rows and isinstance(cost_rows[0], dict) else {}
    monthly_cost = _surreal_monthly_cost(scope, params, since)
    page_sql = (
        "SELECT math::sum(page_count) AS tp FROM documents"  # noqa: S608 -- scope is a static clause; values are bound params
        f"{scope}{' AND' if scope else ' WHERE'} status = 'COMPLETED' GROUP ALL;"  # nosec B608
    )
    page_rows = surreal_db._first_result(surreal_db._run(page_sql, params))
    page_row = page_rows[0] if page_rows and isinstance(page_rows[0], dict) else {}
    status_rows = surreal_db._first_result(
        surreal_db._run(f"SELECT status, count() AS n FROM documents{scope} GROUP BY status;", params)  # nosec B608 # noqa: S608
    )
    status_counts = {str(r.get("status")): int(r.get("n") or 0) for r in (status_rows or []) if isinstance(r, dict)}
    return {
        "total_cost": Decimal(str(cost_row.get("total_cost") or 0)),
        "total_in": int(cost_row.get("tin") or 0),
        "total_out": int(cost_row.get("tout") or 0),
        "total_pages": int(page_row.get("tp") or 0),
        "status_counts": status_counts,
        "monthly_cost": monthly_cost,
    }


def _surreal_monthly_cost(scope: str, params: dict, since: datetime | None) -> Decimal:
    if since is None:
        return Decimal(0)
    month_sql = (
        "SELECT math::sum(cost_usd) AS mc FROM documents"  # noqa: S608 -- scope is a static clause; values are bound params
        f"{scope}{' AND' if scope else ' WHERE'} updated_at >= type::datetime($since) GROUP ALL;"  # nosec B608
    )
    month_rows = surreal_db._first_result(surreal_db._run(month_sql, {**params, "since": since.isoformat()}))
    month_row = month_rows[0] if month_rows and isinstance(month_rows[0], dict) else {}
    return Decimal(str(month_row.get("mc") or 0))


def _get_dashboard_stats(request, document_limit: int | None = None):
    """
    Helper to calculate and format dashboard statistics, avoiding duplicate logic
    between DashboardView and DocumentStatusAPIView.
    """
    currency_details = get_locale_currency_details(request)
    now = timezone.now()
    first_of_month = datetime(now.year, now.month, 1, tzinfo=timezone.UTC)

    user = request.user
    if user.is_staff or user.is_superuser:
        raw_docs = surreal_db.list_document_statuses(limit=document_limit)
    else:
        raw_docs = surreal_db.list_document_statuses(view_common.get_request_actor_id(request), limit=document_limit)
    snapshot_capped = bool(document_limit and len(raw_docs) == document_limit)
    if snapshot_capped:
        raw_docs = raw_docs[:-1]

    # Parse and wrap documents with scoped users_map
    user_ids = {d.get("uploaded_by_id") for d in raw_docs if d and d.get("uploaded_by_id")}
    users_map = view_common._build_users_map(user_ids, fallback_user=user)
    docs = [view_common._wrap_surreal_doc(d, users_map) for d in raw_docs]

    # Unbounded aggregates: the display snapshot may be capped, but spend,
    # tokens, status counts, and pages must reflect every scoped document so
    # the page agrees with the worker budget gate and the status API (AUD-342).
    # monthly_cost uses the same scope as the snapshot (own docs plus unowned),
    # matching the pre-AUD-342 accounting that included unowned documents.
    agg = _get_unbounded_doc_aggregates(request, since=first_of_month)
    monthly_live = agg["monthly_cost"]

    # Add cost of documents that were deleted this month. Offline mode flushes
    # deleted spend into MonthlySpendLog at delete time; production keeps it in
    # document_spend_reconciliations, which total_for_month does not cover —
    # mirror the worker budget gate or the dashboard under-reports spend and
    # can show budget_exceeded=False while the gate rejects work (AUD-394).
    from extractor.models import MonthlySpendLog

    monthly_logged = MonthlySpendLog.total_for_month(now.year, now.month)
    deleted_spend = Decimal(0)
    spend_read_incomplete = False
    if not getattr(settings, "SURREALDB_OFFLINE", False):
        try:
            deleted_total = surreal_db.deleted_document_spend_total(now.year, now.month)
            if deleted_total is None:
                raise RuntimeError("deleted-document spend read failed")
            deleted_spend = Decimal(str(deleted_total))
        except Exception as exc:
            # Do not present the total as complete: flag it so callers can
            # distinguish a verified figure from an under-reported one.
            spend_read_incomplete = True
            logger.warning("[Dashboard] Deleted-document spend read failed; monthly total may under-report: %s", exc)
    monthly_spent = monthly_live + monthly_logged + deleted_spend

    total_spent_usd = agg["total_cost"]
    prompt_tokens = agg["total_in"]
    cand_tokens = agg["total_out"]
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
    stats_dict = {choice[0]: agg["status_counts"].get(choice[0], 0) for choice in status_choices}

    # Total pages
    total_pages = agg["total_pages"]

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
        "spend_read_incomplete": spend_read_incomplete,
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
        SNAPSHOT_CAPPED: snapshot_capped,
    }


def _document_status_display(document: Any) -> str:
    """Return the display label for either an ORM document or wrapped SurrealDB record."""
    status_display = getattr(document, "get_status_display", None)
    if callable(status_display):
        return status_display()
    return document.status.title() if document.status else "Unknown"


def _filter_dashboard_docs(docs: list[Any], query: str) -> list[Any]:
    if not query:
        return docs
    q = query[:200].lower()

    def _text(value: Any) -> str:
        # SurrealDB fields are schema-less: publication_year et al. can arrive
        # as ints, which would make `q in <int>` raise TypeError and 500 the page.
        return str(value).lower() if value is not None else ""

    return [
        d
        for d in docs
        if q in _text(d.title)
        or q in _text(d.author)
        or q in _text(d.language)
        or q in _text(d.document_type)
        or q in _text(getattr(d, "publisher", ""))
        or q in _text(getattr(d, "publication_year", ""))
        or q in _text(getattr(d, "doi", ""))
    ]


def _sort_dashboard_docs(docs: list[Any], sort_by: str) -> list[Any]:
    import datetime

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
    reverse = db_sort.startswith("-")
    key_name = db_sort.lstrip("-")

    key_map = {
        "title": lambda x: (x.title or x.original_filename or "").lower(),
        "cost_usd": lambda x: float(x.cost_usd),
        "status": lambda x: (x.status or "").lower(),
        "created_at": lambda x: x.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC),
    }
    sort_key = key_map.get(key_name, lambda x: x.created_at or datetime.datetime.min.replace(tzinfo=datetime.UTC))
    return sorted(docs, key=sort_key, reverse=reverse)


@method_decorator(never_cache, name="dispatch")
class DashboardView(LoginRequiredMixin, View):
    """
    Renders the unified glassmorphic administrative control dashboard.
    Visualizes token spends, success ratios, and displays a complete search bar
    to interact with the SurrealDB HNSW semantic memory.
    """

    def get(self, request):
        # Probe with limit+1 rows so "exactly at the cap" is not misreported
        # as truncated (AUD-343).
        snapshot_limit = 502
        try:
            stats = _get_dashboard_stats(request, document_limit=snapshot_limit)
        except RuntimeError:
            logger.warning("[Dashboard] Knowledge store unreachable; rendering 503 page.")
            return render(request, "extractor/service_unavailable.html", status=503)
        sort_by = request.GET.get("sort_by", "-date")
        docs = _filter_dashboard_docs(stats["docs"], request.GET.get("q", "").strip())
        sorted_docs = _sort_dashboard_docs(docs, sort_by)

        list_docs = [
            {
                "obj": d,
                "formatted_cost": format_localized_cost(d.cost_usd, stats["currency_details"]),
                "created_at_local": d.created_at.strftime("%Y-%m-%d %H:%M"),
            }
            for d in sorted_docs[:50]
        ]

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
            "snapshot_capped": stats.get(SNAPSHOT_CAPPED, False),
            "currency_details": stats["currency_details"],
            "sort_by": sort_by,
        }
        return render(request, "extractor/dashboard.html", context)


@method_decorator(never_cache, name="dispatch")
class DocumentStatusAPIView(LoginRequiredMixin, View):
    """
    Fast, lightweight JSON API view returning the status of all active/recent documents,
    along with real-time dashboard statistics (monthly budget spent, token counts, success ratio)
    to enable live dynamic updates without full-page reloads.
    """

    def get(self, request):
        try:
            stats = _get_dashboard_stats(request)
        except RuntimeError:
            logger.warning("[StatusAPI] Knowledge store unreachable; returning 503.")
            return JsonResponse({"error": "knowledge_base_unavailable"}, status=503)
        docs = stats["docs"]

        # This is a complete, user-scoped snapshot. Clients use absence as deletion.
        docs_list = []
        for d in docs:
            docs_list.append(
                {
                    "id": d.id,
                    "uuid": d.uuid,
                    "status": d.status,
                    "status_display": _document_status_display(d),
                    "title": d.title,
                    "cost_usd": float(round(d.cost_usd, 6)),
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
            "documents_complete": not stats[SNAPSHOT_CAPPED],
            SNAPSHOT_CAPPED: stats[SNAPSHOT_CAPPED],
            "dashboard_document_ids": [
                str(d.id)
                for d in _sort_dashboard_docs(
                    _filter_dashboard_docs(docs, request.GET.get("q", "").strip()),
                    request.GET.get("sort_by", "-date"),
                )[:50]
            ],
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
                "monthly_spent": float(round(stats["monthly_spent"], 6)),
                "formatted_monthly_spent": stats["formatted_monthly_spent"],
                "formatted_total_spent": stats["formatted_total_spent"],
                "formatted_budget_cap": stats["formatted_budget_cap"],
                "budget_exceeded": stats["budget_exceeded"],
            },
        }
        return JsonResponse(data)
