import json
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from extractor import quiz_studio


class KordaLane2QuizStudioTests(SimpleTestCase):
    def test_generate_quiz_items_persists_hints_rationales_and_mandatory_anchors(self):
        fake_crystals = [
            {
                "crystal_id": "cry-1",
                "title": "Cloud Run Scaling",
                "summary": "Cloud Run scales to zero when minInstanceCount is 0.",
                "anchor_ids": ["anc-1"],
            }
        ]
        llm_payload = {
            "items": [
                {
                    "topic": "Cloud Run Scaling",
                    "difficulty": "Hard",
                    "item_format": "MCQ",
                    "stem": "Which setting guarantees $0 idle compute on Cloud Run?",
                    "choices": ["minInstanceCount: 0", "minInstanceCount: 1", "maxInstanceCount: 0", "cpu: 4"],
                    "correct_answer": "minInstanceCount: 0",
                    "hint": "Look at the lower scaling bound.",
                    "distractor_rationales": {
                        "minInstanceCount: 1": "Keeps 1 warm container billed 24/7.",
                        "maxInstanceCount: 0": "Invalid upper bound.",
                        "cpu: 4": "Controls vCPU allocation, not idle scaling.",
                    },
                    "anchor_ids": ["anc-1"],
                }
            ]
        }
        mock_response = MagicMock(text=json.dumps(llm_payload))
        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = mock_response

        with (
            patch("extractor.llm_gateway._get_vertex_client", return_value=mock_client),
            patch("extractor.surreal_db.save_exam_items", return_value=1) as mock_save,
        ):
            items = quiz_studio.generate_and_persist_quiz(
                owner_id="u1",
                crystals=fake_crystals,
                difficulty="Hard",
                count=1,
                formats=["MCQ"],
                focus_prompt="Focus on serverless cost",
            )

        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["hint"], "Look at the lower scaling bound.")
        self.assertEqual(items[0]["anchor_ids"], ["anc-1"])
        mock_save.assert_called_once()

    def test_mock_exam_session_shuffles_and_hides_answers_until_submit(self):
        stored_items = [
            {
                "item_id": "item-1",
                "stem": "Q1",
                "item_format": "MCQ",
                "choices": ["A", "B", "C", "D"],
                "correct_answer": "A",
                "hint": "Hint 1",
                "distractor_rationales": {"B": "Why not B"},
                "anchor_ids": ["anc-1"],
            },
            {
                "item_id": "item-2",
                "stem": "Q2",
                "item_format": "Short Answer",
                "choices": [],
                "correct_answer": "SurrealDB",
                "hint": "Hint 2",
                "distractor_rationales": {},
                "anchor_ids": ["anc-2"],
            },
        ]
        session_payload = quiz_studio.build_mock_exam_session(stored_items, seed=42)
        self.assertEqual(len(session_payload["questions"]), 2)
        for q in session_payload["questions"]:
            self.assertNotIn("correct_answer", q)
            self.assertNotIn("distractor_rationales", q)
            self.assertIn("hint", q)

    def test_grade_mock_exam_returns_cited_review_with_tombstone_fallback(self):
        stored_items = [
            {
                "item_id": "item-1",
                "stem": "Q1",
                "item_format": "MCQ",
                "choices": ["A", "B"],
                "correct_answer": "A",
                "hint": "Hint 1",
                "distractor_rationales": {"B": "Wrong"},
                "anchor_ids": ["anc-1"],
            }
        ]
        anchors_by_id = {
            "anc-1": {
                "anchor_id": "anc-1",
                "source_id": "src-1",
                "quote": "Verbatim proof quote.",
                "page_num": 2,
                "gcs_status": "purged",
                "sha256": "deadbeef",
            }
        }
        with patch("extractor.surreal_db.record_attempt") as mock_attempt:
            graded = quiz_studio.grade_mock_exam_submission(
                owner_id="u1",
                session_id="sess-1",
                items=stored_items,
                answers={"item-1": "B"},
                anchors_by_id=anchors_by_id,
            )

        self.assertEqual(graded["score"], 0)
        self.assertEqual(graded["total"], 1)
        self.assertFalse(graded["results"][0]["is_correct"])
        self.assertEqual(graded["results"][0]["evidence"][0]["card_type"], "tombstone")
        mock_attempt.assert_called_once()

    def test_export_exam_markdown_toggleable_answer_key(self):
        items = [
            {
                "item_id": "item-1",
                "stem": "What is KORDA?",
                "item_format": "MCQ",
                "choices": ["Refinery", "Podcast"],
                "correct_answer": "Refinery",
                "hint": "Think knowledge.",
                "anchor_ids": ["anc-1"],
            }
        ]
        no_key = quiz_studio.export_exam_markdown(items, include_answers=False)
        with_key = quiz_studio.export_exam_markdown(items, include_answers=True)
        self.assertNotIn("## Answer Key", no_key)
        self.assertIn("## Answer Key", with_key)
        self.assertIn("Refinery", with_key)

    def test_backup_surreal_raises_command_error_when_env_missing(self):
        from django.core.management import call_command
        from django.core.management.base import CommandError

        with (
            patch.dict("os.environ", {"BACKUP_BUCKET_NAME": "", "SURREAL_URL": ""}, clear=False),
            self.assertRaises(CommandError),
        ):
            call_command("backup_surreal")

    def test_quiz_generate_api_enforces_budget_and_rate_limit(self):
        import json
        from types import SimpleNamespace

        from django.test import RequestFactory

        from extractor import views

        rf = RequestFactory()
        req = rf.post(
            "/api/v1/quiz/generate/",
            data=json.dumps({"difficulty": "Medium", "count": 3}),
            content_type="application/json",
        )
        req.user = SimpleNamespace(is_authenticated=True, id=1, username="u1")
        with patch.object(views, "_is_budget_exceeded", return_value=True):
            resp = views.QuizGenerateAPIView.as_view()(req)
        self.assertEqual(resp.status_code, 402)

        with (
            patch.object(views, "_is_budget_exceeded", return_value=False),
            patch.object(views, "_enforce_distributed_rate_limit", return_value=False),
        ):
            resp2 = views.QuizGenerateAPIView.as_view()(req)
        self.assertEqual(resp2.status_code, 429)

    def test_quiz_generate_api_returns_503_when_vertex_unavailable_and_400_on_bad_count(self):
        import json
        from types import SimpleNamespace

        from django.test import RequestFactory

        from extractor import views

        rf = RequestFactory()
        user = SimpleNamespace(is_authenticated=True, id=1, username="u1")

        bad_req = rf.post(
            "/api/v1/quiz/generate/",
            data=json.dumps({"difficulty": "Medium", "count": True}),
            content_type="application/json",
        )
        bad_req.user = user
        with (
            patch.object(views, "_is_budget_exceeded", return_value=False),
            patch.object(views, "_enforce_distributed_rate_limit", return_value=True),
        ):
            bad_resp = views.QuizGenerateAPIView.as_view()(bad_req)
        self.assertEqual(bad_resp.status_code, 400)

        unavail_req = rf.post(
            "/api/v1/quiz/generate/",
            data=json.dumps({"difficulty": "Medium", "count": 2, "crystals": [{"title": "T", "summary": "S"}]}),
            content_type="application/json",
        )
        unavail_req.user = user
        with (
            patch.object(views, "_is_budget_exceeded", return_value=False),
            patch.object(views, "_enforce_distributed_rate_limit", return_value=True),
            patch("extractor.quiz_studio.generate_and_persist_quiz", side_effect=ValueError("Vertex unavailable")),
        ):
            unavail_resp = views.QuizGenerateAPIView.as_view()(unavail_req)
        self.assertEqual(unavail_resp.status_code, 503)

    def test_quiz_submit_api_offline_roundtrip(self):
        import json
        from types import SimpleNamespace

        from django.test import RequestFactory, override_settings

        from extractor import surreal_db, views

        surreal_db._test_korda_exam_items.clear()
        rf = RequestFactory()
        user = SimpleNamespace(is_authenticated=True, id=7, username="u7")
        with override_settings(SURREALDB_OFFLINE=True):
            surreal_db.save_exam_items(
                "7",
                [
                    {
                        "item_id": "item-1",
                        "topic": "Security",
                        "difficulty": "Medium",
                        "item_format": "MCQ",
                        "stem": "Which role?",
                        "choices": ["A", "B"],
                        "correct_answer": "A",
                        "hint": "Least privilege",
                        "distractor_rationales": {"B": "Too broad"},
                        "anchor_ids": [],
                    }
                ],
            )
            req = rf.post(
                "/api/v1/quiz/submit/",
                data=json.dumps({"session_id": "s1", "item_ids": ["item-1"], "answers": {"item-1": "A"}}),
                content_type="application/json",
            )
            req.user = user
            with patch.object(views, "_enforce_distributed_rate_limit", return_value=True):
                resp = views.QuizSubmitAPIView.as_view()(req)
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.content)
        self.assertEqual(data["score"], 1)
        self.assertEqual(data["total"], 1)

    def test_quiz_submit_idempotency_and_rate_endpoint_offline(self):
        import json
        from types import SimpleNamespace

        from django.test import RequestFactory, override_settings

        from extractor import surreal_db, views

        surreal_db._test_korda_exam_items.clear()
        surreal_db._test_korda_attempts.clear()
        rf = RequestFactory()
        user = SimpleNamespace(is_authenticated=True, id=8, username="u8")
        with override_settings(SURREALDB_OFFLINE=True):
            surreal_db.save_exam_items(
                "8",
                [
                    {
                        "item_id": "item-8",
                        "topic": "FSRS",
                        "difficulty": "Easy",
                        "item_format": "Short Answer",
                        "stem": "What does FSRS stand for?",
                        "choices": [],
                        "correct_answer": "Free Spaced Repetition Scheduler",
                        "hint": "Spaced repetition",
                        "distractor_rationales": {},
                        "anchor_ids": [],
                    }
                ],
            )
            with patch.object(views, "_enforce_distributed_rate_limit", return_value=True):
                req1 = rf.post(
                    "/api/v1/quiz/submit/",
                    data=json.dumps(
                        {
                            "session_id": "s-idem",
                            "item_ids": ["item-8"],
                            "answers": {"item-8": "  free spaced   repetition scheduler "},
                        }
                    ),
                    content_type="application/json",
                )
                req1.user = user
                r1 = views.QuizSubmitAPIView.as_view()(req1)
                self.assertEqual(r1.status_code, 200)
                self.assertEqual(json.loads(r1.content)["score"], 1)

                # Resubmitting the same session_id + item_id updates idempotently instead of duplicating.
                r2 = views.QuizSubmitAPIView.as_view()(req1)
                self.assertEqual(r2.status_code, 200)
                self.assertEqual(len(surreal_db._test_korda_attempts), 1)

                # Rate endpoint updates FSRS rating cleanly.
                rate_req = rf.post(
                    "/api/v1/quiz/rate/",
                    data=json.dumps({"session_id": "s-idem", "item_id": "item-8", "rating": 4}),
                    content_type="application/json",
                )
                rate_req.user = user
                rate_resp = views.QuizRateAPIView.as_view()(rate_req)
                self.assertEqual(rate_resp.status_code, 200)
                self.assertEqual(json.loads(rate_resp.content)["rating"], 4)
                self.assertEqual(surreal_db._test_korda_exam_items["item-8"]["fsrs_reps"], 2)
