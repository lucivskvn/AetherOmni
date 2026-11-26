import logging
from typing import Any

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import JsonResponse, StreamingHttpResponse
from django.views import View

from extractor import view_common
from extractor.utils import (
    query_semantic_knowledge_rag,
    render_markdown_to_html,
)

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

RAG_RATE_LIMIT_MSG = "Search rate limit reached. Please wait a moment before sending another query."


class DocumentRAGSearchView(LoginRequiredMixin, View):
    """
    AJAX endpoint running global semantic search.
    Embeds query, searches SurrealDB, and generates answers.
    """

    def _check_rag_limits(self, request):
        rl_key = f"rag_ratelimit_{view_common.get_request_actor_id(request)}"
        if not view_common._enforce_distributed_rate_limit(f"rag:{rl_key}", rl_key, max_requests=10, window_seconds=60):
            logger.warning("[RAGRateLimit] SurrealDB rate limit fallback error or limit exceeded for %s", rl_key)
            return JsonResponse(
                {"error": RAG_RATE_LIMIT_MSG},
                status=429,
            )

        if view_common._is_budget_exceeded(request.user, view_common.get_request_actor_id(request)):
            return JsonResponse(
                {"error": "Monthly AI budget has been reached. Intelligent search is paused until the budget resets."},
                status=402,
            )
        return None

    def _parse_document_ids(self, document_ids_str):
        if not document_ids_str:
            return None
        document_ids = []
        for i in document_ids_str.split(",")[:50]:
            i_clean = i.strip()[:64]
            if i_clean:
                if ":" in i_clean:
                    i_clean = i_clean.split(":", 1)[1]
                try:
                    document_ids.append(int(i_clean))
                except ValueError:
                    document_ids.append(i_clean)
        return document_ids[:50]

    def get(self, request):
        query = request.GET.get("q", "").strip()[:1000]
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
                actor_id=view_common.get_request_actor_id(request),
            )
            # Render markdown answer safely to HTML
            results["answer_html"] = render_markdown_to_html(results["answer"])
            return JsonResponse(results, encoder=view_common.AetherJSONEncoder)
        except ValueError as e:
            logger.warning("[RAG Search] Validation error: %s", e)
            return JsonResponse({"error": "Invalid search parameters provided."}, status=400)
        except Exception as e:
            logger.exception("[RAG Search] Internal exception during semantic search: %s", e)
            return JsonResponse(
                {"error": "An unexpected error occurred during search. Please try again later."}, status=500
            )


class StreamQueryRAGView(LoginRequiredMixin, View):
    """
    Server-Sent Events (SSE) streaming endpoint for Real-Time RAG answers.
    Dispatches query across hybrid HNSW+BM25 index and streams token chunks.
    """

    def _check_stream_limits(self, request, actor_id: str):
        rl_key = f"rag_ratelimit_{actor_id}"
        if not view_common._enforce_distributed_rate_limit(
            f"stream:{rl_key}", rl_key, max_requests=10, window_seconds=60
        ):
            logger.warning("[RAGStreamRateLimit] SurrealDB rate limit fallback error or limit exceeded for %s", rl_key)
            return JsonResponse(
                {"error": RAG_RATE_LIMIT_MSG},
                status=429,
            )

        if view_common._is_budget_exceeded(request.user, actor_id):
            return JsonResponse(
                {"error": "Monthly AI budget has been reached. Intelligent search is paused until the budget resets."},
                status=402,
            )
        return None

    def _parse_doc_ids(self, document_ids_str: str) -> list[Any] | None:
        if not document_ids_str:
            return None
        ids: list[Any] = []
        for i in document_ids_str.split(","):
            i_clean = i.strip()
            if not i_clean:
                continue
            if ":" in i_clean:
                i_clean = i_clean.split(":", 1)[1]
            i_clean = i_clean[:64]
            try:
                ids.append(int(i_clean))
            except ValueError:
                ids.append(i_clean)
        return ids[:50]

    def get(self, request):
        from extractor.rag import stream_query_rag

        query = request.GET.get("q", "").strip()[:1000]
        if not query:
            return JsonResponse({"error": "Empty search query."}, status=400)

        actor_id = view_common.get_request_actor_id(request)
        limit_resp = self._check_stream_limits(request, actor_id)
        if limit_resp:
            return limit_resp

        document_ids = self._parse_doc_ids(request.GET.get("document_ids", "").strip())
        if document_ids:
            document_ids = document_ids[:50]

        from django.utils.functional import SimpleLazyObject

        user = request.user
        if isinstance(user, SimpleLazyObject):
            user = user._wrapped

        generator = stream_query_rag(
            query=query,
            document_ids=document_ids,
            top_k=5,
            user=user,
            actor_id=actor_id,
        )

        response = StreamingHttpResponse(generator, content_type="text/event-stream")
        response["Cache-Control"] = "no-cache"
        response["X-Accel-Buffering"] = "no"
        response["Connection"] = "keep-alive"
        response["Content-Encoding"] = "none"
        return response
