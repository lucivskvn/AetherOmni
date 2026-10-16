import io
import json
import subprocess
from contextlib import redirect_stderr
from unittest import TestCase
from unittest.mock import patch

from scripts import verify_gcp_prerequisites as preflight

PROJECT = "sample-project"
PROJECT_NUMBER = "123456789"
RUNTIME = f"korda-runtime@{PROJECT}.iam.gserviceaccount.com"
INVOKER = f"korda-tasks-invoker@{PROJECT}.iam.gserviceaccount.com"
RUNTIME_MEMBER = f"serviceAccount:{RUNTIME}"
INVOKER_MEMBER = f"serviceAccount:{INVOKER}"
AGENT_MEMBER = f"serviceAccount:service-{PROJECT_NUMBER}@gcp-sa-cloudtasks.iam.gserviceaccount.com"


class DeploymentPrerequisitesTests(TestCase):
    def setUp(self):
        self.bucket_policy = {"bindings": [{"role": "roles/storage.objectAdmin", "members": [RUNTIME_MEMBER]}]}
        self.identity_policy = {
            "bindings": [{"role": "roles/iam.serviceAccountUser", "members": [RUNTIME_MEMBER, AGENT_MEMBER]}]
        }
        self.worker_policy = {"bindings": [{"role": "roles/run.invoker", "members": [INVOKER_MEMBER]}]}
        self.queue_policy = {"bindings": [{"role": "roles/cloudtasks.taskDeleter", "members": [RUNTIME_MEMBER]}]}
        self.disabled = set()
        patcher = patch.object(preflight, "_metadata", side_effect=self.metadata)
        self.metadata_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def metadata(self, _project, *arguments):
        if arguments[:3] == ("storage", "buckets", "describe"):
            return {}
        if arguments[:3] == ("iam", "service-accounts", "describe"):
            return {"email": arguments[3], "disabled": arguments[3] in self.disabled}
        if arguments[:3] == ("storage", "buckets", "get-iam-policy"):
            return self.bucket_policy
        if arguments[:3] == ("iam", "service-accounts", "get-iam-policy"):
            return self.identity_policy
        if arguments[:3] == ("run", "services", "get-iam-policy"):
            return self.worker_policy
        if arguments[:3] == ("tasks", "queues", "get-iam-policy"):
            return self.queue_policy
        self.fail(f"Unexpected metadata request: {arguments}")

    def verify(self):
        preflight.verify(PROJECT, PROJECT_NUMBER, "asia-southeast1", "asia-southeast2", "extractor-tasks-v2")

    def test_complete_resource_scoped_contract_passes(self):
        self.verify()
        self.metadata_mock.assert_any_call(
            PROJECT, "tasks", "queues", "get-iam-policy", "extractor-tasks-v2", "--location=asia-southeast2"
        )
        self.metadata_mock.assert_any_call(
            PROJECT, "run", "services", "get-iam-policy", "korda-worker", "--region=asia-southeast1"
        )

    def test_missing_role_bindings_fail_closed(self):
        for policy in (self.bucket_policy, self.identity_policy, self.worker_policy, self.queue_policy):
            with self.subTest(policy=policy):
                original = policy["bindings"]
                policy["bindings"] = []
                with self.assertRaisesRegex(preflight.PreflightError, "Missing unconditional"):
                    self.verify()
                policy["bindings"] = original

    def test_task_creator_and_service_agent_both_need_act_as(self):
        for missing in (RUNTIME_MEMBER, AGENT_MEMBER):
            with self.subTest(missing=missing):
                self.identity_policy["bindings"][0]["members"] = [
                    member for member in (RUNTIME_MEMBER, AGENT_MEMBER) if member != missing
                ]
                with self.assertRaisesRegex(preflight.PreflightError, "roles/iam.serviceAccountUser"):
                    self.verify()

    def test_conditional_binding_does_not_prove_permission(self):
        self.worker_policy["bindings"][0]["condition"] = {"expression": "false"}
        with self.assertRaisesRegex(preflight.PreflightError, "roles/run.invoker"):
            self.verify()

    def test_disabled_service_accounts_fail_closed(self):
        for account in (RUNTIME, INVOKER):
            with self.subTest(account=account):
                self.disabled = {account}
                with self.assertRaisesRegex(preflight.PreflightError, "disabled or unconfirmed"):
                    self.verify()

    def test_invalid_project_number_fails_before_metadata_access(self):
        with self.assertRaisesRegex(preflight.PreflightError, "numeric GCP project number"):
            preflight.verify(PROJECT, "not-a-number", "region", "queue-region", "queue")
        self.metadata_mock.assert_not_called()


class MetadataLookupTests(TestCase):
    @patch.object(preflight.shutil, "which", return_value="/usr/bin/gcloud")
    @patch.object(preflight.subprocess, "run")
    def test_lookup_uses_only_metadata_and_separate_arguments(self, mock_run, _mock_which):
        mock_run.return_value = subprocess.CompletedProcess([], 0, json.dumps({"disabled": False}), "")
        self.assertEqual(
            preflight._metadata(PROJECT, "iam", "service-accounts", "describe", INVOKER), {"disabled": False}
        )
        self.assertEqual(
            mock_run.call_args.args[0],
            [
                "/usr/bin/gcloud",
                "iam",
                "service-accounts",
                "describe",
                INVOKER,
                f"--project={PROJECT}",
                "--format=json",
            ],
        )
        self.assertEqual(mock_run.call_args.kwargs["timeout"], 30)

    @patch.object(preflight.shutil, "which", return_value=None)
    def test_missing_cli_fails_closed(self, _mock_which):
        with self.assertRaisesRegex(preflight.PreflightError, "gcloud is required"):
            preflight._metadata(PROJECT, "storage", "buckets", "describe", "bucket")

    @patch.object(preflight.shutil, "which", return_value="/usr/bin/gcloud")
    @patch.object(preflight.subprocess, "run")
    def test_timeout_and_command_failure_fail_closed(self, mock_run, _mock_which):
        for error in (OSError("unavailable"), subprocess.TimeoutExpired("gcloud", 30)):
            with self.subTest(error=type(error).__name__):
                mock_run.side_effect = error
                with self.assertRaises(preflight.PreflightError):
                    preflight._metadata(PROJECT, "iam", "service-accounts", "describe", INVOKER)

    @patch.object(preflight.shutil, "which", return_value="/usr/bin/gcloud")
    @patch.object(preflight.subprocess, "run")
    def test_unreadable_or_invalid_metadata_fails_closed_without_echoing_output(self, mock_run, _mock_which):
        for code, output in ((1, "private-diagnostic"), (0, "not-json"), (0, "[]")):
            with self.subTest(code=code, output=output):
                mock_run.return_value = subprocess.CompletedProcess([], code, output, "private-diagnostic")
                with self.assertRaises(preflight.PreflightError) as raised:
                    preflight._metadata(PROJECT, "iam", "service-accounts", "describe", INVOKER)
                self.assertNotIn("private-diagnostic", str(raised.exception))

    @patch.object(preflight, "verify", side_effect=preflight.PreflightError("missing IAM binding"))
    def test_cli_returns_failure_for_missing_deployment_contract(self, _mock_verify):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = preflight.main(
                [
                    "--project",
                    PROJECT,
                    "--project-number",
                    PROJECT_NUMBER,
                    "--region",
                    "region",
                    "--queue-region",
                    "queue-region",
                ]
            )
        self.assertEqual(result, 1)
        self.assertIn("missing IAM binding", stderr.getvalue())
