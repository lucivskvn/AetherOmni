"""Unit tests for pure helpers in the surreal_* satellite modules."""

from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from extractor import surreal_audit, surreal_cache, surreal_db, surreal_exam, surreal_provenance


class RateLimitResultParsingTests(SimpleTestCase):
    def test_extract_stmt_decision_bool_passthrough(self):
        self.assertIs(surreal_cache._extract_stmt_decision(True, 10), True)
        self.assertIs(surreal_cache._extract_stmt_decision(False, 10), False)

    def test_extract_stmt_decision_list_of_bool(self):
        self.assertIs(surreal_cache._extract_stmt_decision([True], 10), True)
        self.assertIs(surreal_cache._extract_stmt_decision([False, True], 10), False)

    def test_extract_stmt_decision_request_count_threshold(self):
        within = [{"request_count": 3}]
        over = [{"request_count": 11}]
        self.assertIs(surreal_cache._extract_stmt_decision(within, 10), True)
        self.assertIs(surreal_cache._extract_stmt_decision(over, 10), False)

    def test_extract_stmt_decision_unrecognized_shapes(self):
        self.assertIsNone(surreal_cache._extract_stmt_decision(None, 10))
        self.assertIsNone(surreal_cache._extract_stmt_decision("yes", 10))
        self.assertIsNone(surreal_cache._extract_stmt_decision([{"other": 1}], 10))
        self.assertIsNone(surreal_cache._extract_stmt_decision([], 10))

    def test_parse_rate_limit_transaction_result_bool(self):
        self.assertIs(surreal_cache._parse_rate_limit_transaction_result(True, 10), True)

    def test_parse_rate_limit_transaction_result_dict_result(self):
        res = [{"result": [{"request_count": 2}]}]
        self.assertIs(surreal_cache._parse_rate_limit_transaction_result(res, 10), True)

    def test_parse_rate_limit_transaction_result_scans_candidates(self):
        res = [{"status": "OK"}, {"result": False}]
        self.assertIs(surreal_cache._parse_rate_limit_transaction_result(res, 10), False)

    def test_parse_rate_limit_transaction_result_no_decision(self):
        self.assertIsNone(surreal_cache._parse_rate_limit_transaction_result([{"x": 1}], 10))


class MonthlySpendLedgerTests(SimpleTestCase):
    def test_monthly_spend_ledger_total_empty_result(self):
        with (
            patch("extractor.surreal_db._run", return_value=[]),
            patch("extractor.surreal_db._first_result", return_value=[]),
        ):
            self.assertEqual(
                surreal_audit.monthly_spend_ledger_total(2026, 1),
                {"cost_usd": 0.0, "input_tokens": 0.0, "output_tokens": 0.0},
            )

    def test_monthly_spend_ledger_total_sums_row(self):
        row = {"cost_usd": 1.5, "input_tokens": 100, "output_tokens": 50}
        with (
            patch("extractor.surreal_db._run", return_value=[{"result": [row]}]),
            patch("extractor.surreal_db._first_result", return_value=[row]),
        ):
            self.assertEqual(
                surreal_audit.monthly_spend_ledger_total(2026, 1),
                {"cost_usd": 1.5, "input_tokens": 100.0, "output_tokens": 50.0},
            )

    def test_monthly_spend_ledger_total_run_failure_returns_none(self):
        with patch("extractor.surreal_db._run", side_effect=RuntimeError("db down")):
            self.assertIsNone(surreal_audit.monthly_spend_ledger_total(2026, 1))

    def test_monthly_spend_ledger_add_coerces_params(self):
        with patch("extractor.surreal_db._run", return_value=[{"result": []}]) as mock_run:
            self.assertTrue(surreal_audit.monthly_spend_ledger_add("2026", "1", "2.5", "10", "20"))
        params = mock_run.call_args[0][1]
        self.assertEqual(params["year"], 2026)
        self.assertEqual(params["month"], 1)
        self.assertEqual(params["cost_usd"], 2.5)
        self.assertEqual(params["input_tokens"], 10)
        self.assertEqual(params["output_tokens"], 20)

    def test_monthly_spend_ledger_add_failure_returns_false(self):
        with patch("extractor.surreal_db._run", side_effect=RuntimeError("db down")):
            self.assertFalse(surreal_audit.monthly_spend_ledger_add(2026, 1, 1.0, 1, 1))


class ExamItemPayloadTests(SimpleTestCase):
    def test_normalize_exam_item_payload_defaults(self):
        out = surreal_exam._normalize_exam_item_payload({"item_id": 42})
        self.assertEqual(out["item_id"], "42")
        self.assertEqual(out["difficulty"], "Medium")
        self.assertEqual(out["item_format"], "MCQ")
        self.assertEqual(out["choices"], [])
        self.assertEqual(out["anchor_ids"], [])

    def test_normalize_exam_item_payload_preserves_values(self):
        raw = {
            "item_id": "i-1",
            "topic": "Calculus",
            "difficulty": "Hard",
            "item_format": "FRQ",
            "stem": "Find the limit",
            "choices": ["a", "b"],
            "correct_answer": ["a"],
            "hint": "L'Hopital",
            "distractor_rationales": {"b": "wrong rule"},
            "anchor_ids": ["anc-1"],
        }
        out = surreal_exam._normalize_exam_item_payload(raw)
        self.assertEqual(out["stem"], "Find the limit")
        self.assertEqual(out["correct_answer"], ["a"])
        self.assertEqual(out["distractor_rationales"], {"b": "wrong rule"})
        self.assertEqual(out["anchor_ids"], ["anc-1"])


class OfflineExamStoreTests(SimpleTestCase):
    def setUp(self):
        surreal_db._test_korda_exam_items.clear()

    def tearDown(self):
        surreal_db._test_korda_exam_items.clear()

    @override_settings(SURREALDB_OFFLINE=True)
    def test_save_exam_items_offline_merges_existing(self):
        items = [
            {"item_id": "i-1", "stem": "first", "difficulty": "Easy"},
            {"item_id": "i-2", "stem": "second", "difficulty": "Hard"},
        ]
        surreal_db._test_korda_exam_items["i-1"] = {"owner_id": "u1", "fsrs_stability": 2.5}
        saved = surreal_exam.save_exam_items("u1", items, crystal_ids=["c-1"])
        self.assertEqual(saved, 2)
        row = surreal_db._test_korda_exam_items["i-1"]
        self.assertEqual(row["stem"], "first")
        self.assertEqual(row["fsrs_stability"], 2.5)
        self.assertEqual(row["owner_id"], "u1")
        self.assertEqual(row["crystal_ids"], ["c-1"])

    @override_settings(SURREALDB_OFFLINE=True)
    def test_save_exam_items_empty_returns_zero(self):
        self.assertEqual(surreal_exam.save_exam_items("u1", []), 0)

    @override_settings(SURREALDB_OFFLINE=True)
    def test_list_exam_items_offline_filters_by_owner_and_difficulty(self):
        surreal_exam.save_exam_items(
            "u1",
            [
                {"item_id": "i-1", "difficulty": "Easy", "stem": "easy"},
                {"item_id": "i-2", "difficulty": "Hard", "stem": "hard"},
            ],
        )
        surreal_exam.save_exam_items("u2", [{"item_id": "i-3", "difficulty": "Easy", "stem": "other"}])
        rows = surreal_exam.list_exam_items("u1", difficulty="Hard", due_only=False)
        self.assertEqual([r["item_id"] for r in rows], ["i-2"])

    @override_settings(SURREALDB_OFFLINE=True)
    def test_list_exam_items_offline_due_only_falls_back_to_all_when_none_due(self):
        from datetime import UTC, datetime, timedelta

        items = [{"item_id": "i-future", "difficulty": "Easy", "stem": "future"}]
        surreal_exam.save_exam_items("u1", items)
        surreal_db._test_korda_exam_items["i-future"]["fsrs_due_at"] = datetime.now(UTC) + timedelta(days=1)
        rows = surreal_exam.list_exam_items("u1", due_only=True)
        # due_only prioritizes due rows but falls back to all rows when none are due
        self.assertEqual([r["item_id"] for r in rows], ["i-future"])

    @override_settings(SURREALDB_OFFLINE=True)
    def test_list_exam_items_offline_due_only_returns_due_subset(self):
        from datetime import UTC, datetime, timedelta

        items = [
            {"item_id": "i-due", "difficulty": "Easy", "stem": "due"},
            {"item_id": "i-future", "difficulty": "Easy", "stem": "future"},
        ]
        surreal_exam.save_exam_items("u1", items)
        surreal_db._test_korda_exam_items["i-due"]["fsrs_due_at"] = datetime.now(UTC) - timedelta(days=1)
        surreal_db._test_korda_exam_items["i-future"]["fsrs_due_at"] = datetime.now(UTC) + timedelta(days=1)
        rows = surreal_exam.list_exam_items("u1", due_only=True)
        self.assertEqual([r["item_id"] for r in rows], ["i-due"])


class AnchorPayloadTests(SimpleTestCase):
    def test_build_anchor_payloads_generates_ids_and_filters_none(self):
        anchors = [
            {"quote": "q1", "page_num": 3, "embedding": None},
            {"anchor_id": "custom", "quote": "q2", "start_sec": 12.5},
        ]
        out = surreal_provenance._build_anchor_payloads("src-1", 2, anchors)
        self.assertEqual(out[0]["anchor_id"], "src-1:v2:0")
        self.assertEqual(out[0]["page_num"], 3)
        self.assertNotIn("embedding", out[0])
        self.assertEqual(out[1]["anchor_id"], "custom")
        self.assertEqual(out[1]["start_sec"], 12.5)

    def test_build_anchor_payloads_empty(self):
        self.assertEqual(surreal_provenance._build_anchor_payloads("s", 1, []), [])
