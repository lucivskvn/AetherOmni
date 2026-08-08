from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor.deployment import (
    get_gcp_access_token,
    get_gcp_project_details,
    get_service_config,
    get_service_logs,
    run_qa_checks,
    update_service_scale,
)


class DeploymentFunctionsTestCase(TestCase):
    """Verifies internal functions in extractor/deployment.py."""

    @patch("os.getenv")
    def test_get_gcp_project_details_env(self, mock_getenv):
        # Env vars exist
        mock_getenv.side_effect = lambda key, default=None: {
            "GCP_PROJECT_ID": "env-project",
            "GCP_REGION": "us-east1",
        }.get(key, default)

        details = get_gcp_project_details()
        self.assertEqual(details["project_id"], "env-project")
        self.assertEqual(details["region"], "us-east1")

    @patch("subprocess.run")
    @patch("os.getenv")
    @patch("urllib.request.urlopen")
    def test_get_gcp_project_details_metadata(self, mock_urlopen, mock_getenv, mock_subproc):
        # Env vars don't exist, metadata server returns project and region
        mock_getenv.side_effect = lambda key, default=None: default
        mock_subproc.return_value = MagicMock(stdout="", returncode=1)

        mock_resp_project = MagicMock()
        mock_resp_project.read.return_value = b"metadata-project"
        mock_resp_project.__enter__.return_value = mock_resp_project

        mock_resp_region = MagicMock()
        mock_resp_region.read.return_value = b"projects/123/regions/us-west2"
        mock_resp_region.__enter__.return_value = mock_resp_region

        mock_urlopen.side_effect = [mock_resp_project, mock_resp_region]

        details = get_gcp_project_details()
        self.assertEqual(details["project_id"], "metadata-project")
        self.assertEqual(details["region"], "us-west2")

    @patch("subprocess.run")
    @patch("os.getenv")
    @patch("urllib.request.urlopen")
    def test_get_gcp_project_details_fallback(self, mock_urlopen, mock_getenv, mock_subproc):
        # Env vars don't exist, metadata server fails, gcloud fails
        mock_getenv.side_effect = lambda key, default=None: default
        mock_subproc.return_value = MagicMock(stdout="", returncode=1)
        mock_urlopen.side_effect = Exception("Metadata server offline")

        details = get_gcp_project_details()
        self.assertIsNone(details["project_id"])
        self.assertEqual(details["region"], "asia-southeast1")

    @patch("urllib.request.urlopen")
    def test_get_gcp_access_token_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"access_token": "mock-gcp-oauth-test-token"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        token = get_gcp_access_token()
        self.assertEqual(token, "mock-gcp-oauth-test-token")

    @patch("urllib.request.urlopen")
    def test_get_gcp_access_token_failure(self, mock_urlopen):
        mock_urlopen.side_effect = Exception("Metadata server offline")
        token = get_gcp_access_token()
        self.assertIsNone(token)

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("urllib.request.urlopen")
    def test_get_service_config_api(self, mock_urlopen, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = "mock-token"
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"kind": "Service", "metadata": {"name": "test-service"}}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        config = get_service_config("test-service")
        self.assertEqual(config["metadata"]["name"], "test-service")

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("subprocess.check_output")
    def test_get_service_config_gcloud(self, mock_check_output, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = None
        mock_check_output.return_value = b'{"kind": "Service", "metadata": {"name": "test-service-local"}}'

        config = get_service_config("test-service")
        self.assertEqual(config["metadata"]["name"], "test-service-local")
        mock_check_output.assert_called_once()
        cmd = mock_check_output.call_args[0][0]
        self.assertIn("gcloud", cmd)
        self.assertIn("describe", cmd)

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("extractor.deployment.get_service_config")
    @patch("urllib.request.urlopen")
    def test_update_service_scale_api(self, mock_urlopen, mock_get_config, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = "mock-token"
        mock_get_config.return_value = {
            "metadata": {"name": "worker", "uid": "123", "resourceVersion": "abc"},
            "spec": {"template": {"metadata": {"annotations": {}}}},
            "status": {"ready": True},
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"status": "success"}'
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        res = update_service_scale("worker", 1, 5)
        self.assertEqual(res, {"status": "success"})

        # Verify PUT body removed restricted fields
        req = mock_urlopen.call_args[0][0]
        data_sent = req.data.decode("utf-8")
        import json

        config_sent = json.loads(data_sent)
        self.assertNotIn("status", config_sent)
        self.assertNotIn("uid", config_sent["metadata"])
        self.assertEqual(
            config_sent["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/minScale"], "1"
        )
        self.assertEqual(
            config_sent["spec"]["template"]["metadata"]["annotations"]["autoscaling.knative.dev/maxScale"], "5"
        )

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("extractor.deployment.get_service_config")
    @patch("subprocess.check_output")
    def test_update_service_scale_gcloud(self, mock_check_output, mock_get_config, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = None
        mock_get_config.return_value = {
            "metadata": {"name": "worker"},
            "spec": {"template": {"metadata": {"annotations": {}}}},
        }
        mock_check_output.return_value = b"Service updated"

        res = update_service_scale("worker", 0, 3)
        self.assertEqual(res["status"], "success")
        mock_check_output.assert_called_once()
        cmd = mock_check_output.call_args[0][0]
        self.assertIn("--min-instances", cmd)
        self.assertIn("0", cmd)
        self.assertIn("--max-instances", cmd)
        self.assertIn("3", cmd)

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("urllib.request.urlopen")
    def test_get_service_logs_api(self, mock_urlopen, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = "mock-token"
        mock_resp = MagicMock()
        mock_resp.read.return_value = (
            b'{"entries": [{"timestamp": "2026", "textPayload": "Hello Log", "severity": "INFO"}]}'
        )
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        logs = get_service_logs("worker")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["message"], "Hello Log")

    @patch("extractor.deployment.get_gcp_project_details")
    @patch("extractor.deployment.get_gcp_access_token")
    @patch("subprocess.check_output")
    def test_get_service_logs_gcloud(self, mock_check_output, mock_get_token, mock_get_details):
        mock_get_details.return_value = {"project_id": "test-project", "region": "asia-southeast1"}
        mock_get_token.return_value = None
        mock_check_output.side_effect = [b'[{"timestamp": "2026", "textPayload": "Local Log", "severity": "INFO"}]']

        logs = get_service_logs("worker")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["message"], "Local Log")

    @patch("subprocess.check_output")
    def test_run_qa_checks(self, mock_check_output):
        mock_check_output.side_effect = [b"ruff check output", b"ruff format output", b"django check output"]
        report = run_qa_checks()
        self.assertIn("✓ [Ruff Linter]", report)
        self.assertIn("✓ [Ruff Formatter]", report)
        self.assertIn("✓ [Django System Check]", report)
