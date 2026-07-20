from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor import cloud_tasks


class CloudTasksTestCase(TestCase):
    """
    Verifies that the Cloud Tasks client wrapper enqueues tasks correctly,
    falls back to local execution/emulator when necessary, and handles routing.
    """

    @patch("extractor.cloud_tasks.logger")
    def test_enqueue_local_thread_fallback(self, mock_logger):
        # We test that in local environment (where get_gcp_project_details returns None)
        # the task triggers execution inside a local Thread pool fallback
        with patch("extractor.cloud_tasks.get_gcp_project_details") as mock_details:
            mock_details.return_value = {"project_id": None, "region": "us-central1"}

            # Let's mock the actual target task runner in extractor.tasks
            # to verify it gets invoked inside the fallback thread
            mock_target = MagicMock()
            with patch.dict("extractor.cloud_tasks._LOCAL_TASK_REGISTRY", {"process_document": mock_target}):
                cloud_tasks.enqueue("process_document", {"document_id": 42})

                # Wait for local thread pool execution to complete (we join or sleep a brief moment)
                import time

                time.sleep(0.5)

                mock_target.assert_called_once_with({"document_id": 42})

    @patch("extractor.cloud_tasks.get_gcp_project_details")
    @patch("extractor.cloud_tasks.tasks_v2.CloudTasksClient")
    def test_enqueue_gcp_cloud_tasks_api(self, mock_client_class, mock_details):
        mock_details.return_value = {
            "project_id": "my-gcp-project",
            "region": "asia-southeast1",
            "web_service": "aether-web",
            "worker_service": "aether-worker",
        }

        mock_client = MagicMock()
        mock_client_class.return_value = mock_client

        # Mock project ID to return dummy
        with self.settings(DEBUG=False, APP_URL="https://my-app.run.app", WORKER_URL="https://my-app.run.app"):
            cloud_tasks.enqueue("process_document", {"document_id": 99})

            # Check that the Cloud Tasks client was used to create a task
            mock_client.create_task.assert_called_once()
            args, kwargs = mock_client.create_task.call_args
            parent = kwargs.get("parent") or args[0]
            self.assertIn("projects/my-gcp-project/locations/asia-southeast1/queues/", parent)

            task = kwargs.get("task") or args[1]
            self.assertEqual(task["http_request"]["url"], "https://my-app.run.app/internal/tasks/process_document/")
            self.assertEqual(task["http_request"]["headers"]["Content-Type"], "application/json")
