import json
from unittest.mock import MagicMock, patch

from django.test import RequestFactory, TestCase, override_settings

from extractor import task_handlers
from extractor.task_handlers import CloudTaskHandlerView


class TaskHandlersTestCase(TestCase):
    """Direct unit tests for task_handlers.py."""

    def setUp(self):
        self.factory = RequestFactory()

    def test_register_populates_registry(self):
        task_handlers._register()
        self.assertIn("process_document", task_handlers.TASK_REGISTRY)
        self.assertIn("reembed_document", task_handlers.TASK_REGISTRY)
        self.assertIn("cleanup_expired_documents", task_handlers.TASK_REGISTRY)
        self.assertIn("reap_stale_tasks", task_handlers.TASK_REGISTRY)
        self.assertIn("store_user_memory", task_handlers.TASK_REGISTRY)

    @override_settings(DEBUG=True)
    def test_verify_oidc_token_debug(self):
        request = self.factory.post("/internal/tasks/test_task/")
        self.assertTrue(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False)
    def test_verify_oidc_token_missing_or_malformed_header(self):
        # Missing auth header
        req_missing = self.factory.post("/internal/tasks/test_task/")
        self.assertFalse(task_handlers._verify_oidc_token(req_missing, "audience"))

        # Malformed auth header
        req_bad = self.factory.post("/internal/tasks/test_task/")
        req_bad.META["HTTP_AUTHORIZATION"] = "Basic abcdef"
        self.assertFalse(task_handlers._verify_oidc_token(req_bad, "audience"))

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

    @override_settings(DEBUG=False, CLOUD_TASKS_SERVICE_ACCOUNT="tasks@example.iam.gserviceaccount.com")
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_multiple_audiences_fallback(self, mock_verify):
        # First candidate raises exception, second candidate succeeds
        def side_effect(token, req, aud):
            if aud == "bad_aud":
                raise ValueError("Audience mismatch")
            return {
                "iss": "accounts.google.com",
                "email": "tasks@example.iam.gserviceaccount.com",
                "email_verified": True,
            }

        mock_verify.side_effect = side_effect
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertTrue(task_handlers._verify_oidc_token(request, ["", "bad_aud", "good_aud"]))

    @override_settings(DEBUG=False)
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_multiple_audiences_all_fail(self, mock_verify):
        mock_verify.side_effect = ValueError("Invalid token")
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertFalse(task_handlers._verify_oidc_token(request, ["aud1", "aud2"]))

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

    @override_settings(DEBUG=False, CLOUD_TASKS_SERVICE_ACCOUNT="tasks@example.iam.gserviceaccount.com")
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_rejects_unverified_email(self, mock_verify):
        mock_verify.return_value = {
            "iss": "https://accounts.google.com",
            "email": "tasks@example.iam.gserviceaccount.com",
            "email_verified": False,
        }
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertFalse(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False, CLOUD_TASKS_SERVICE_ACCOUNT="")
    @patch("google.oauth2.id_token.verify_oauth2_token")
    def test_verify_oidc_token_rejects_empty_service_account_in_production(self, mock_verify):
        mock_verify.return_value = {
            "iss": "https://accounts.google.com",
            "email": "attacker@example.com",
            "email_verified": True,
        }
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_AUTHORIZATION"] = "Bearer token123"
        self.assertFalse(task_handlers._verify_oidc_token(request, "audience"))

    @override_settings(DEBUG=False)
    @patch("google.auth.transport.requests.Request", side_effect=RuntimeError("Auth init failed"))
    def test_verify_oidc_token_top_level_exception(self, _mock_request):
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
    def test_verify_source_ip_forwarded_for(self):
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["HTTP_X_FORWARDED_FOR"] = "35.199.10.5, 10.0.0.1"
        self.assertTrue(task_handlers._verify_source_ip(request))

    @override_settings(DEBUG=False)
    def test_verify_source_ip_invalid_ip_format(self):
        request = self.factory.post("/internal/tasks/test_task/")
        request.META["REMOTE_ADDR"] = "not-an-ip"
        self.assertFalse(task_handlers._verify_source_ip(request))

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
        with (
            patch("extractor.task_handlers._verify_oidc_token", return_value=True),
            patch("extractor.task_handlers._verify_source_ip", return_value=True),
        ):
            response = CloudTaskHandlerView().post(request, "process_document")
            self.assertEqual(response.status_code, 200)
            mock_handler.assert_called_once_with({"doc_id": 1})

    def test_post_dispatch_unknown_task_returns_404(self):
        request = self.factory.post(
            "/internal/tasks/non_existent_task/", data=json.dumps({}), content_type="application/json"
        )
        with (
            patch("extractor.task_handlers._verify_oidc_token", return_value=True),
            patch("extractor.task_handlers._verify_source_ip", return_value=True),
        ):
            response = CloudTaskHandlerView().post(request, "non_existent_task")
            self.assertEqual(response.status_code, 404)

    def test_post_dispatch_invalid_json_returns_400(self):
        task_handlers.TASK_REGISTRY["json_test"] = lambda p: None
        try:
            request = self.factory.post(
                "/internal/tasks/json_test/", data="not valid json", content_type="application/json"
            )
            with (
                patch("extractor.task_handlers._verify_oidc_token", return_value=True),
                patch("extractor.task_handlers._verify_source_ip", return_value=True),
            ):
                response = CloudTaskHandlerView().post(request, "json_test")
                self.assertEqual(response.status_code, 400)
        finally:
            task_handlers.TASK_REGISTRY.pop("json_test", None)

    def test_post_dispatch_failed_oidc_returns_401(self):
        task_handlers.TASK_REGISTRY["auth_test"] = lambda p: None
        try:
            request = self.factory.post("/internal/tasks/auth_test/", data="{}", content_type="application/json")
            with (
                patch("extractor.task_handlers._verify_oidc_token", return_value=False),
                patch("extractor.task_handlers._verify_source_ip", return_value=True),
            ):
                response = CloudTaskHandlerView().post(request, "auth_test")
                self.assertEqual(response.status_code, 401)
        finally:
            task_handlers.TASK_REGISTRY.pop("auth_test", None)

    def test_post_dispatch_unrecognized_ip_proceeds(self):
        task_handlers.TASK_REGISTRY["ip_test"] = lambda p: None
        try:
            request = self.factory.post("/internal/tasks/ip_test/", data="{}", content_type="application/json")
            with (
                patch("extractor.task_handlers._verify_oidc_token", return_value=True),
                patch("extractor.task_handlers._verify_source_ip", return_value=False),
            ):
                response = CloudTaskHandlerView().post(request, "ip_test")
                self.assertEqual(response.status_code, 200)
                data = json.loads(response.content)
                self.assertEqual(data.get("status"), "ok")
        finally:
            task_handlers.TASK_REGISTRY.pop("ip_test", None)

    def test_post_dispatch_handler_exception_returns_200_with_error_status(self):
        def failing_handler(payload):
            raise RuntimeError("Task execution failed")

        task_handlers.TASK_REGISTRY["failing_task"] = failing_handler
        try:
            request = self.factory.post("/internal/tasks/failing_task/", data="{}", content_type="application/json")
            with (
                patch("extractor.task_handlers._verify_oidc_token", return_value=True),
                patch("extractor.task_handlers._verify_source_ip", return_value=True),
            ):
                response = CloudTaskHandlerView().post(request, "failing_task")
                self.assertEqual(response.status_code, 200)
                data = json.loads(response.content)
                self.assertEqual(data.get("status"), "error")
        finally:
            task_handlers.TASK_REGISTRY.pop("failing_task", None)
