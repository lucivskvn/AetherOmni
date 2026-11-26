"""Unit tests for pure helpers in the surreal_* satellite modules."""

from unittest.mock import patch

from django.test import SimpleTestCase

from extractor import surreal_audit, surreal_cache


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
