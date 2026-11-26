import logging
from datetime import datetime
from decimal import Decimal
from typing import Any

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
        raw_docs = surreal_db.list_documents(limit=document_limit)
    else:
        raw_docs = surreal_db.list_documents(view_common.get_request_actor_id(request), limit=document_limit)
    snapshot_capped = bool(document_limit and len(raw_docs) == document_limit)
    if snapshot_capped:
        raw_docs = raw_docs[:-1]

    # Parse and wrap documents with scoped users_map
    user_ids = {d.get("uploaded_by_id") for d in raw_docs if d and d.get("uploaded_by_id")}
    users_map = view_common._build_users_map(user_ids, fallback_user=user)
    docs = [view_common._wrap_surreal_doc(d, users_map) for d in raw_docs]

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
    return [
        d
        for d in docs
        if q in (d.title or "").lower()
        or q in (d.author or "").lower()
        or q in (d.language or "").lower()
        or q in (d.document_type or "").lower()
        or q in (getattr(d, "publisher", "") or "").lower()
        or q in (getattr(d, "publication_year", "") or "").lower()
        or q in (getattr(d, "doi", "") or "").lower()
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
        snapshot_limit = 501
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
            "currency_details": stats["currency_details"],
            "sort_by": sort_by,
        }
        return render(request, "extractor/dashboard.html", context)


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
