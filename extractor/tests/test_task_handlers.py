import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, override_settings

from extractor import task_handlers
from extractor.task_handlers import CloudTaskHandlerView


class TaskHandlersTestCase(TestCase):
    """Direct unit tests for task_handlers.py."""

    def setUp(self):
        self.factory = RequestFactory()

    @override_settings(DEBUG=True)
    def test_verify_oidc_token_debug(self):
        request = self.factory.post("/internal/tasks/test_task/")
        self.assertTrue(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False, APP_URL="https://app.example.test", WORKER_URL="https://worker.example.test")
    @patch("extractor.task_handlers._verify_source_ip", return_value=True)
    @patch("extractor.task_handlers._verify_oidc_token", return_value=True)
    def test_worker_url_is_used_as_task_oidc_audience(self, mock_verify_oidc, _mock_verify_source_ip):
        task_handlers.TASK_REGISTRY["audience_test"] = lambda payload: None
        request = self.factory.post("/internal/tasks/audience_test/", data="{}", content_type="application/json")
        try:
            response = CloudTaskHandlerView().post(request, "audience_test")
        finally:
            task_handlers.TASK_REGISTRY.pop("audience_test", None)

        self.assertEqual(response.status_code, 200)
        mock_verify_oidc.assert_called_once()
        call_args = mock_verify_oidc.call_args[0]
        self.assertEqual(call_args[0], request)
        self.assertIn("https://worker.example.test/internal/tasks/audience_test/", call_args[1])

    @override_settings(DEBUG=False, CLOUD_TASKS_SERVICE_ACCOUNT="tasks@example.iam.gserviceaccount.com")
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_prod_success(self, mock_verify):
        mock_verify.return_value = {
            "iss": "https://accounts.google.com",
            "email": "tasks@example.iam.gserviceaccount.com",
            "email_verified": True,
        }
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertTrue(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False)
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_prod_invalid_iss(self, mock_verify):
        mock_verify.return_value = {"iss": "bad-issuer.com"}
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertFalse(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False, CLOUD_TASKS_SERVICE_ACCOUNT="tasks@example.iam.gserviceaccount.com")
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_rejects_unexpected_service_account(self, mock_verify):
        mock_verify.return_value = {
            "iss": "https://accounts.google.com",
            "email": "other@example.iam.gserviceaccount.com",
            "email_verified": True,
        }
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertFalse(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=True)
    def test_verify_source_ip_debug(self):
        request = self.factory.post("/internal/tasks/test_task/")
        self.assertTrue(task_handlers._verify_source_ip(request))

    @override_settings(DEBUG=False)
    def test_verify_source_ip_prod_valid(self):
        # 35.199.0.1 is in 35.199.0.0/16
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["REMOTE_ADDR"] = "35.199.0.1"
        self.assertTrue(task_handlers._verify_source_ip(request))

    @override_settings(DEBUG=False)
    def test_verify_source_ip_prod_invalid(self):
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["REMOTE_ADDR"] = "192.168.1.1"
        self.assertFalse(task_handlers._verify_source_ip(request))

    @patch("extractor.task_handlers.TASK_REGISTRY")
    def test_post_dispatch_success(self, mock_registry):
        mock_handler = MagicMock()
        mock_registry.get.return_value = mock_handler
        mock_registry.__contains__.return_value = True

        request = self.factory.post(
            "/internal/tasks/process_document/", data=json.dumps({"doc_id": 1}), content_type="application/json"
        )
        # Mock security checks
        with (
            patch("extractor.task_handlers._verify_oidc_token", return_value=True),
            patch("extractor.task_handlers._verify_source_ip", return_value=True),
        ):
            response = CloudTaskHandlerView().post(request, "process_document")
            self.assertEqual(response.status_code, 200)
            mock_handler.assert_called_once_with({"doc_id": 1})
