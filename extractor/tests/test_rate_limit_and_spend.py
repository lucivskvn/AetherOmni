from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from extractor import surreal_db, views


class DistributedRateLimitAndSpendTestCase(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(DEBUG=False, SURREALDB_OFFLINE=False)
    def test_production_rag_rate_limit_uses_surrealdb_not_locmem(self):
        request = self.factory.post("/rag-search/")
        request.user = MagicMock(id=42, is_authenticated=True, email="u@example.com")
        request.session = {}
        view = views.DocumentRAGSearchView()

        with patch("extractor.surreal_db.check_rate_limit_atomic", return_value=False) as mock_rl:
            resp = view._check_rag_limits(request)
        mock_rl.assert_called_once()
        self.assertIsNotNone(resp)
        self.assertEqual(resp.status_code, 429)

    @override_settings(DEBUG=False, SURREALDB_OFFLINE=False)
    def test_production_export_rate_limit_fails_closed_on_surreal_error(self):
        request = self.factory.get("/export/", REMOTE_ADDR="198.51.100.10")
        request.user = MagicMock(id=42, is_authenticated=True, email="u@example.com")
        request.session = {}

        with patch("extractor.surreal_db.check_rate_limit_atomic", side_effect=RuntimeError("db down")):
            allowed = views._reserve_export_rate_limit(request)
        self.assertFalse(allowed)

    @override_settings(SURREALDB_OFFLINE=False)
    def test_get_user_total_spend_uses_aggregate_query(self):
        since = datetime(2026, 11, 1, tzinfo=UTC)
        with patch("extractor.surreal_db._run", return_value=[{"total": 4.25}]) as mock_run:
            total = surreal_db.get_user_total_spend("user-123", since=since)

        self.assertEqual(total, Decimal("4.25"))
        sql_sent = mock_run.call_args[0][0]
        self.assertIn("math::sum(cost_usd)", sql_sent)
        self.assertNotIn("SELECT *", sql_sent)
