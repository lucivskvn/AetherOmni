import logging

from django.contrib.auth.mixins import LoginRequiredMixin
from django.http import HttpResponse, JsonResponse
from django.views import View

from extractor import surreal_db, view_common

logger = logging.getLogger(view_common.VIEWS_LOGGER_NAME)

CONTENT_DISPOSITION_ATTACHMENT = "attachment"


def _apply_markdown_download_headers(resp: HttpResponse, filename: str) -> HttpResponse:
    resp["Content-Disposition"] = f'{CONTENT_DISPOSITION_ATTACHMENT}; filename="{filename}"'
    resp["X-Content-Type-Options"] = "nosniff"
    resp["Cache-Control"] = "private, no-store, no-transform"
    return resp


def _load_quiz_crystals(actor_id: str, body: dict) -> list[dict]:

    doc_slug = str(body.get("doc_slug") or "").strip()[:120] or None
    crystals = surreal_db.list_crystals_for_user(actor_id, doc_slug=doc_slug, limit=50)
    raw_cids = body.get("crystal_ids")
    if crystals and isinstance(raw_cids, list) and raw_cids:
        wanted = {str(c) for c in raw_cids if c}
        filtered = [c for c in crystals if str(c.get("crystal_id") or "") in wanted]
        if filtered:
            return filtered[:20]
    if crystals or not isinstance(body.get("crystals"), list):
        return crystals[:20]
    return [
        {
            "crystal_id": "",
            "title": str(c.get("title") or "")[:200],
            "summary": str(c.get("summary") or "")[:2000],
            "anchor_ids": [],
        }
        for c in body["crystals"][:10]
        if isinstance(c, dict) and (c.get("title") or c.get("summary"))
    ]


class StrictLibrarianAPIView(LoginRequiredMixin, View):
    """Zero-LLM Strict Librarian Mode returning verbatim anchors + deterministic coordinates."""

    def post(self, request):
        from extractor import rag

        body, err = view_common._parse_json_dict_body(request)
        if err is not None or body is None:
            return err

        query = str(body.get("query") or "").strip()[:1000]
        if not query:
            return JsonResponse({"error": "Query is required."}, status=400)

        limit = view_common._parse_positive_int(body.get("limit"), default=10, min_val=1, max_val=30)
        if limit is None:
            return JsonResponse({"error": "Invalid limit."}, status=400)
        doc_slug = str(body.get("doc_slug") or "").strip()[:120] or None

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"librarian:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=30, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        try:
            return JsonResponse(rag.strict_librarian_search(query, owner_id=actor_id, limit=limit, doc_slug=doc_slug))
        except Exception as exc:
            logger.exception("[StrictLibrarian] Search failed for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)


from django.views.generic import TemplateView as _TemplateView


class QuizStudioView(LoginRequiredMixin, _TemplateView):
    """Distraction-free Quiz Studio & Timed Mock Exam Runner."""

    template_name = "extractor/quiz_runner.html"

    def get_context_data(self, **kwargs):
        from extractor import quiz_studio

        ctx = super().get_context_data(**kwargs)
        actor_id = view_common.get_request_actor_id(self.request)
        try:
            items = surreal_db.list_exam_items(actor_id, due_only=True, limit=30)
            crystals = surreal_db.list_crystals_for_user(actor_id, limit=50)
        except Exception as exc:
            logger.warning("[QuizStudio] Failed to load items/crystals for actor=%s: %s", actor_id, exc)
            items = []
            crystals = []
        ctx["exam_session"] = quiz_studio.build_mock_exam_session(items)
        ctx["item_count"] = len(items)
        ctx["crystals"] = crystals
        return ctx


class QuizGenerateAPIView(LoginRequiredMixin, View):
    """Generate and persist Quiz Studio items grounded in verified crystals."""

    def post(self, request):
        from extractor import llm_gateway, quiz_studio

        body, err = view_common._parse_json_dict_body(request)
        if err is not None or body is None:
            return err

        actor_id = view_common.get_request_actor_id(request)
        if view_common._is_budget_exceeded(request.user, actor_id):
            return JsonResponse({"error": view_common.ERR_BUDGET_EXCEEDED}, status=402)
        rl_key = f"quiz_gen:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=10, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        count = view_common._parse_positive_int(body.get("count"), default=5, min_val=1, max_val=30)
        if count is None:
            return JsonResponse({"error": "Invalid count."}, status=400)

        raw_formats = body.get("formats") or ["MCQ"]
        if not isinstance(raw_formats, list):
            return JsonResponse({"error": "formats must be a list."}, status=400)

        weights = body.get("blueprint_weights")
        try:
            crystals = _load_quiz_crystals(actor_id, body)
            if not crystals:
                return JsonResponse({"error": "No verified crystals available for quiz generation."}, status=400)
            items = quiz_studio.generate_and_persist_quiz(
                owner_id=actor_id,
                crystals=crystals,
                difficulty=str(body.get("difficulty") or "Medium"),
                count=count,
                formats=[str(f) for f in raw_formats[:4]],
                focus_prompt=str(body.get("focus_prompt") or "")[:500],
                blueprint_weights=weights if isinstance(weights, dict) else None,
            )
        except (ValueError, RuntimeError, llm_gateway.APIError) as exc:
            logger.warning("[QuizGenerate] Generation unavailable for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": "Quiz generation service temporarily unavailable."}, status=503)
        except Exception as exc:
            logger.exception("[QuizGenerate] Unexpected error for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)

        return JsonResponse(
            {"status": "ok", "count": len(items), "session": quiz_studio.build_mock_exam_session(items)}
        )


class QuizSubmitAPIView(LoginRequiredMixin, View):
    """Grade Mock Exam submission at zero LLM cost and return Cited Review + Tombstone cards."""

    def post(self, request):
        from extractor import quiz_studio

        body, err = view_common._parse_json_dict_body(request)
        if err is not None or body is None:
            return err
        if not isinstance(body.get("answers"), dict):
            return JsonResponse({"error": "Invalid answers payload."}, status=400)

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"quiz_submit:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=30, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        session_id = str(body.get("session_id") or "").strip()[:64] or uuid.uuid4().hex[:16]
        answers = body["answers"]
        raw_item_ids = body.get("item_ids")
        target_ids = (
            [str(i) for i in raw_item_ids if i][:50]
            if isinstance(raw_item_ids, list)
            else [str(k) for k in answers][:50]
        )
        if not target_ids:
            return JsonResponse({"error": "No quiz items submitted."}, status=400)
        raw_ratings = body.get("ratings")
        ratings = (
            {str(k): int(v) for k, v in raw_ratings.items() if isinstance(v, int) and not isinstance(v, bool)}
            if isinstance(raw_ratings, dict)
            else None
        )
        try:
            items = surreal_db.get_exam_items_by_ids(actor_id, target_ids)
            if not items:
                return JsonResponse({"error": "No matching exam items found."}, status=404)
            anchor_ids = list({aid for item in items for aid in (item.get("anchor_ids") or []) if aid})
            anchors = surreal_db.get_anchors_by_ids(anchor_ids, actor_id)
            graded = quiz_studio.grade_mock_exam_submission(
                owner_id=actor_id,
                session_id=session_id,
                items=items,
                answers=answers,
                anchors_by_id=anchors,
                ratings=ratings,
            )
        except Exception as exc:
            logger.exception("[QuizSubmit] Grading failed for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)
        return JsonResponse(graded)


class QuizRateAPIView(LoginRequiredMixin, View):
    """Update FSRS self-rating (1=Again..4=Easy) for a graded exam item."""

    def post(self, request):

        body, err = view_common._parse_json_dict_body(request)
        if err is not None or body is None:
            return err

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"quiz_rate:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=60, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        session_id = str(body.get("session_id") or "").strip()[:64]
        item_id = str(body.get("item_id") or "").strip()[:64]
        rating = view_common._parse_positive_int(body.get("rating"), default=3, min_val=1, max_val=4)
        if not session_id or not item_id or rating is None or body.get("rating") is None:
            return JsonResponse({"error": "session_id, item_id, and rating (1-4) are required."}, status=400)

        try:
            updated = surreal_db.update_attempt_rating(
                session_id=session_id,
                item_id=item_id,
                owner_id=actor_id,
                rating=rating,
            )
        except Exception as exc:
            logger.exception("[QuizRate] Rating update failed for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)
        if not updated:
            return JsonResponse({"error": "Attempt not found."}, status=404)
        return JsonResponse({"status": "ok", "item_id": item_id, "rating": rating})


class QuizExportMarkdownView(LoginRequiredMixin, View):
    """Export persistent exam items as a Markdown Exam Pack with toggleable Answer Key."""

    def get(self, request):
        from extractor import quiz_studio

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"quiz_export:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=20, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        include_answers = request.GET.get("answers", "1") != "0"
        try:
            items = surreal_db.list_exam_items(actor_id, due_only=False, limit=100)
        except Exception as exc:
            logger.exception("[QuizExport] Failed to list exam items for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)
        md = quiz_studio.export_exam_markdown(items, include_answers=include_answers)
        resp = HttpResponse(md, content_type="text/markdown; charset=utf-8")
        return _apply_markdown_download_headers(resp, "korda_exam_pack.md")


def _parse_crystal_synth_payload(body: dict) -> tuple[dict | None, JsonResponse | None]:
    title = str(body.get("title") or "").strip()[:200]
    summary = str(body.get("summary") or "").strip()[:5000]
    doc_slug = str(body.get("doc_slug") or "general").strip()[:120] or "general"
    raw_aids = body.get("anchor_ids")
    if raw_aids is not None and not isinstance(raw_aids, list):
        return None, JsonResponse({"error": "anchor_ids must be a list."}, status=400)
    raw_tags = body.get("tags")
    if raw_tags is not None and not isinstance(raw_tags, list):
        return None, JsonResponse({"error": "tags must be a list."}, status=400)
    if not title or not summary:
        return None, JsonResponse({"error": "title and summary are required."}, status=400)
    return {
        "title": title,
        "summary": summary,
        "doc_slug": doc_slug,
        "anchor_ids": list(dict.fromkeys(str(a) for a in (raw_aids or []) if a))[:50],
        "tags": [str(t)[:60] for t in (raw_tags or []) if t][:20],
    }, None


class CrystalSynthesizeAPIView(LoginRequiredMixin, View):
    """Synthesize and persist a verified Crystal grounded in existing Anchors."""

    def post(self, request):
        import uuid

        body, err = view_common._parse_json_dict_body(request)
        if err is not None or body is None:
            return err

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"crystal_synth:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=20, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        parsed, parse_err = _parse_crystal_synth_payload(body)
        if parse_err is not None or parsed is None:
            return parse_err

        try:
            crystal = surreal_db.create_crystal_with_provenance(
                crystal_id=uuid.uuid4().hex[:16],
                owner_id=actor_id,
                doc_slug=parsed["doc_slug"],
                title=parsed["title"],
                summary=parsed["summary"],
                anchor_ids=parsed["anchor_ids"],
                status="verified",
                tags=parsed["tags"],
            )
        except Exception as exc:
            logger.exception("[CrystalSynthesize] Failed for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)
        return JsonResponse({"status": "ok", "crystal": crystal})


def _serve_markdown_download(request, bundles: list[str]) -> HttpResponse:
    if not bundles:
        return JsonResponse({"error": "No verified crystals available to compile."}, status=404)
    raw_part = request.GET.get("part")
    if raw_part is None:
        part_num = 1
    else:
        try:
            part_num = int(raw_part)
        except ValueError:
            return JsonResponse({"error": "Invalid bundle part."}, status=400)
        if part_num < 1 or part_num > len(bundles):
            return JsonResponse({"error": "Bundle part not found."}, status=404)
    resp = HttpResponse(bundles[part_num - 1], content_type="text/markdown; charset=utf-8")
    return _apply_markdown_download_headers(resp, f"korda_kb_part_{part_num}.md")


class MarkdownCompileAPIView(LoginRequiredMixin, View):
    """Compile verified crystals & verbatim anchors into NotebookLM-optimized Markdown bundles."""

    def get(self, request):
        from extractor import compiler

        actor_id = view_common.get_request_actor_id(request)
        rl_key = f"compile_md:{actor_id}"
        if not view_common._enforce_distributed_rate_limit(rl_key, rl_key, max_requests=20, window_seconds=60):
            return JsonResponse({"error": view_common.ERR_RATE_LIMIT}, status=429)

        doc_slug = (request.GET.get("doc_slug") or "").strip()[:120] or None
        try:
            crystals = surreal_db.list_crystals_for_user(actor_id, doc_slug=doc_slug, limit=200)
            anchor_ids = list({aid for c in crystals for aid in (c.get("anchor_ids") or []) if aid})
            anchors_by_id = surreal_db.get_anchors_by_ids(anchor_ids, actor_id)
        except Exception as exc:
            logger.exception("[MarkdownCompile] Failed for actor=%s: %s", actor_id, exc)
            return JsonResponse({"error": view_common.ERR_SERVICE_UNAVAILABLE}, status=503)

        for crystal in crystals:
            crystal["anchors"] = [
                anchors_by_id[aid] for aid in (crystal.get("anchor_ids") or []) if aid in anchors_by_id
            ]
        bundles = compiler.compile_markdown_bundles(crystals)
        if request.GET.get("download") == "1":
            return _serve_markdown_download(request, bundles)
        return JsonResponse({"status": "ok", "bundle_count": len(bundles), "bundles": bundles})
