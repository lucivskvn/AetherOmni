import os
from unittest.mock import MagicMock, patch

import httpx
from django.test import TestCase

from scripts import init_surreal


class InitSurrealTestCase(TestCase):
    """Direct unit tests for scripts/init_surreal.py functions."""

    @patch("scripts.init_surreal.time.sleep")
    def test_wait_for_surreal_success(self, mock_sleep):
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp

        ready = init_surreal.wait_for_surreal(mock_client, max_retries=3)
        self.assertTrue(ready)
        self.assertEqual(mock_client.get.call_count, 1)

    @patch("scripts.init_surreal.time.sleep")
    def test_wait_for_surreal_retry_then_success(self, mock_sleep):
        mock_client = MagicMock(spec=httpx.Client)
        mock_resp_fail = MagicMock(spec=httpx.Response)
        mock_resp_fail.status_code = 500
        mock_resp_ok = MagicMock(spec=httpx.Response)
        mock_resp_ok.status_code = 200

        mock_client.get.side_effect = [Exception("Refused"), mock_resp_fail, mock_resp_ok]

        ready = init_surreal.wait_for_surreal(mock_client, max_retries=5)
        self.assertTrue(ready)
        self.assertEqual(mock_client.get.call_count, 3)

    @patch("scripts.init_surreal.time.sleep")
    def test_wait_for_surreal_timeout(self, mock_sleep):
        mock_client = MagicMock(spec=httpx.Client)
        mock_client.get.side_effect = Exception("Refused")

        ready = init_surreal.wait_for_surreal(mock_client, max_retries=3)
        self.assertFalse(ready)
        self.assertEqual(mock_client.get.call_count, 3)

    @patch("scripts.init_surreal.os.path.exists")
    @patch("builtins.open")
    def test_apply_schema_success(self, mock_open, mock_exists):
        mock_exists.return_value = True
        mock_file = MagicMock()
        mock_file.read.return_value = "DEFINE TABLE test;"
        mock_open.return_value.__enter__.return_value = mock_file

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"status": "OK"}]
        mock_client.post.return_value = mock_resp

        init_surreal.apply_schema(mock_client)
        # Should be called 3 times now: 1 for Namespace, 1 for DB, 1 for schema SQL
        self.assertEqual(mock_client.post.call_count, 3)

    @patch("scripts.init_surreal.os.path.exists")
    @patch("builtins.open")
    def test_apply_schema_with_errors(self, mock_open, mock_exists):
        mock_exists.return_value = True
        mock_file = MagicMock()
        mock_file.read.return_value = "DEFINE TABLE test; DEFINR TABLE error;"
        mock_open.return_value.__enter__.return_value = mock_file

        mock_client = MagicMock(spec=httpx.Client)
        mock_resp = MagicMock(spec=httpx.Response)
        mock_resp.status_code = 200
        mock_resp.json.return_value = [{"status": "OK"}, {"status": "ERR", "detail": "Syntax error"}]
        mock_client.post.return_value = mock_resp

        init_surreal.apply_schema(mock_client)
        self.assertEqual(mock_client.post.call_count, 3)

    @patch.dict(os.environ, {"SURREALDB_OFFLINE": "True"})
    @patch("scripts.init_surreal.httpx.Client")
    def test_main_offline(self, mock_httpx_client):
        # Should return immediately and not initialize httpx client
        init_surreal.main()
        mock_httpx_client.assert_not_called()

    def test_local_superuser_bootstrap_does_not_log_email(self):
        admin_email = "bootstrap-admin@example.com"

        with self.assertLogs("init_surreal", level="INFO") as logs:
            init_surreal._create_local_superuser_stub(admin_email)

        self.assertNotIn(admin_email, "\n".join(logs.output))
