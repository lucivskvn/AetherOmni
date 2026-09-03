from decimal import Decimal
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase, override_settings

from extractor.llm_gateway import (
    BudgetExceededException,
    GeminiProcessingError,
    UnifiedResponse,
    _call_openrouter,
    _parse_refinement_output,
    calculate_gemini_cost,
    calculate_openrouter_cost,
    check_budget_and_api_limit,
    extract_retry_delay,
    fetch_realtime_model_pricing,
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
            model_used="gemini-2.5-flash",
        )
        self.assertEqual(resp.input_tokens, 100)
        self.assertEqual(resp.output_tokens, 50)
        self.assertEqual(resp.cost_usd, Decimal("0.0015"))
        self.assertEqual(resp.text, "Hello World")
        self.assertEqual(resp.model_used, "gemini-2.5-flash")

    def test_calculate_gemini_cost(self):
        cost = calculate_gemini_cost("gemini-2.5-flash", 1000, 500)
        self.assertGreater(cost, Decimal("0"))
        self.assertIsInstance(cost, Decimal)

    def test_calculate_openrouter_cost(self):
        cost = calculate_openrouter_cost("google/gemini-2.5-flash", 1000, 500)
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

    @patch("extractor.models.MonthlySpendLog.total_for_month", return_value=Decimal("4.00"))
    @patch("extractor.models.SourceDocument.objects.filter")
    @patch("extractor.models.SystemSettings.get_settings")
    @override_settings(SURREALDB_OFFLINE=True)
    def test_check_budget_exceeded_with_deleted_documents(self, mock_settings, mock_filter, mock_spend):
        """Test that spend from deleted documents (flushed to MonthlySpendLog) causes budget limit exception when combined with live spend."""
        mock_settings_obj = MagicMock()
        mock_settings_obj.monthly_budget_usd = Decimal("5.00")
        mock_settings.return_value = mock_settings_obj

        mock_aggregate = MagicMock()
        mock_aggregate.aggregate.return_value = {"total": Decimal("1.50")}
        mock_filter.return_value = mock_aggregate

        with self.assertRaises(BudgetExceededException):
            check_budget_and_api_limit()

    @patch("extractor.llm_gateway._get_cached_realtime_pricing", return_value=None)
    @patch("extractor.llm_gateway.httpx.get")
    def test_fetch_realtime_model_pricing_success(self, mock_httpx_get, mock_get_cached):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "data": [
                {
                    "id": "google/gemini-2.5-flash",
                    "pricing": {"prompt": "0.00000015", "completion": "0.00000060"},
                }
            ]
        }
        mock_httpx_get.return_value = mock_response

        pricing = fetch_realtime_model_pricing()

        mock_httpx_get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "KORDA-Platform/1.5", "HTTP-Referer": "https://korda.app"},
            timeout=5.0,
            verify=True,
        )
        self.assertIsNotNone(pricing)
        self.assertIn("google/gemini-2.5-flash", pricing)
        self.assertEqual(pricing["google/gemini-2.5-flash"]["prompt"], Decimal("0.00000015"))
        self.assertEqual(pricing["google/gemini-2.5-flash"]["completion"], Decimal("0.00000060"))

    @patch("extractor.llm_gateway._get_cached_realtime_pricing")
    @patch("extractor.llm_gateway.httpx.get")
    def test_fetch_realtime_model_pricing_cached(self, mock_httpx_get, mock_get_cached):
        cached_data = {"test-model": {"prompt": Decimal("0.001"), "completion": Decimal("0.002")}}
        mock_get_cached.return_value = cached_data

        pricing = fetch_realtime_model_pricing()

        mock_get_cached.assert_called_once()
        mock_httpx_get.assert_not_called()
        self.assertEqual(pricing, cached_data)

    @patch("extractor.llm_gateway._get_cached_realtime_pricing", return_value=None)
    @patch("extractor.llm_gateway.httpx.get", side_effect=httpx.HTTPError("Request failed"))
    def test_fetch_realtime_model_pricing_http_error(self, mock_httpx_get, mock_get_cached):
        pricing = fetch_realtime_model_pricing()

        mock_httpx_get.assert_called_once_with(
            "https://openrouter.ai/api/v1/models",
            headers={"User-Agent": "KORDA-Platform/1.5", "HTTP-Referer": "https://korda.app"},
            timeout=5.0,
            verify=True,
        )
        self.assertIsNone(pricing)

    @patch("extractor.llm_gateway.httpx.post")
    @patch("extractor.llm_gateway._get_openrouter_api_key", return_value="test-key-123")
    def test_call_openrouter_enforces_ssl_verification(self, mock_key, mock_post):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "choices": [{"message": {"content": "Response from OpenRouter"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 20},
        }
        mock_post.return_value = mock_response

        res = _call_openrouter("Hello prompt", "System instruction", "openrouter/free")

        mock_post.assert_called_once()
        _, kwargs = mock_post.call_args
        self.assertTrue(kwargs.get("verify"))
        self.assertEqual(kwargs.get("timeout"), 60.0)
        self.assertEqual(res.text, "Response from OpenRouter")
        self.assertEqual(res.input_tokens, 10)
        self.assertEqual(res.output_tokens, 20)

    @patch("extractor.llm_gateway.httpx.post", side_effect=Exception("Connection reset"))
    @patch("extractor.llm_gateway._get_openrouter_api_key", return_value="test-key-123")
    def test_call_openrouter_handles_exception(self, mock_key, mock_post):
        with self.assertRaises(GeminiProcessingError):
            _call_openrouter("Hello prompt", None, "openrouter/free")
