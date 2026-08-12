from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from extractor.llm_gateway import (
    BudgetExceededException,
    UnifiedResponse,
    _parse_refinement_output,
    calculate_gemini_cost,
    calculate_openrouter_cost,
    check_budget_and_api_limit,
    extract_retry_delay,
    is_rate_limit_error,
)


class LLMGatewayTestCase(TestCase):
    """Verifies LLM Gateway helper methods, budget limits, and cost calculations."""

    def test_unified_response_properties(self):
        resp = UnifiedResponse(
            text="Hello World",
            in_toks=100,
            out_toks=50,
            cost_val=Decimal("0.0015"),
            model_used="gemini-3.6-flash",
        )
        self.assertEqual(resp.input_tokens, 100)
        self.assertEqual(resp.output_tokens, 50)
        self.assertEqual(resp.cost_usd, Decimal("0.0015"))
        self.assertEqual(resp.text, "Hello World")
        self.assertEqual(resp.model_used, "gemini-3.6-flash")

    def test_calculate_gemini_cost(self):
        cost = calculate_gemini_cost("gemini-3.6-flash", 1000, 500)
        self.assertGreater(cost, Decimal("0"))
        self.assertIsInstance(cost, Decimal)

    def test_calculate_openrouter_cost(self):
        cost = calculate_openrouter_cost("google/gemini-3.6-flash", 1000, 500)
        self.assertGreater(cost, Decimal("0"))
        self.assertIsInstance(cost, Decimal)

    def test_is_rate_limit_error(self):
        err_429 = Exception("429 Too Many Requests")
        err_quota = Exception("ResourceHasBeenExhausted: quota exceeded")
        err_generic = Exception("Internal Server Error")

        self.assertTrue(is_rate_limit_error(err_429))
        self.assertTrue(is_rate_limit_error(err_quota))
        self.assertFalse(is_rate_limit_error(err_generic))

    def test_extract_retry_delay(self):
        err = Exception("Please retry in 12.5s")
        delay = extract_retry_delay(err)
        self.assertEqual(delay, 12.5)

        err_json = Exception('"retryDelay": "15s"')
        self.assertEqual(extract_retry_delay(err_json), 15.0)

        err_no_delay = Exception("Generic error with no delay")
        self.assertIsNone(extract_retry_delay(err_no_delay))

    def test_refinement_output_removes_complete_markdown_heading_before_qa_block(self):
        refined_text, yaml_block, qa_list = _parse_refinement_output(
            'Introduction\n# Model-generated heading\nDetails\n```json\n[{"question": "Q", "answer": "A"}]\n```'
        )

        self.assertEqual(refined_text, "Introduction\nDetails")
        self.assertEqual(yaml_block, "")
        self.assertEqual(qa_list, [{"question": "Q", "answer": "A"}])

    @patch("extractor.models.MonthlySpendLog.total_for_month", return_value=Decimal("1.00"))
    @patch("extractor.models.SourceDocument.objects.filter")
    @patch("extractor.models.SystemSettings.get_settings")
    @override_settings(SURREALDB_OFFLINE=True)
    def test_check_budget_under_limit(self, mock_settings, mock_filter, mock_spend):
        mock_settings_obj = MagicMock()
        mock_settings_obj.monthly_budget_usd = Decimal("10.00")
        mock_settings.return_value = mock_settings_obj

        mock_aggregate = MagicMock()
        mock_aggregate.aggregate.return_value = {"total": Decimal("2.50")}
        mock_filter.return_value = mock_aggregate

        # Should pass cleanly without raising exception
        check_budget_and_api_limit()

    @patch("extractor.models.MonthlySpendLog.total_for_month", return_value=Decimal("5.00"))
    @patch("extractor.models.SourceDocument.objects.filter")
    @patch("extractor.models.SystemSettings.get_settings")
    @override_settings(SURREALDB_OFFLINE=True)
    def test_check_budget_exceeded(self, mock_settings, mock_filter, mock_spend):
        mock_settings_obj = MagicMock()
        mock_settings_obj.monthly_budget_usd = Decimal("5.00")
        mock_settings.return_value = mock_settings_obj

        mock_aggregate = MagicMock()
        mock_aggregate.aggregate.return_value = {"total": Decimal("10.00")}
        mock_filter.return_value = mock_aggregate

        with self.assertRaises(BudgetExceededException):
            check_budget_and_api_limit()
