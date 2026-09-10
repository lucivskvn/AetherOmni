"""Regression coverage for cancellation, durable delivery, and complete UI snapshots."""

import json
from datetime import timedelta
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone
from google.api_core.exceptions import NotFound

from extractor import cloud_tasks, surreal_db, task_handlers
from extractor.models import SourceDocument
from extractor.views import (
    DocumentDeleteView,
    DocumentRAGSearchView,
    SFTDatasetPreviewView,
    _find_existing_doc_by_hash,
)


class DashboardReliabilityTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_superuser(username="reliability-admin", password="test-password")
        self.client.force_login(self.user)

    def test_status_snapshot_includes_documents_beyond_previous_limit(self):
        SourceDocument.objects.bulk_create(
            [SourceDocument(original_filename=f"document-{i}.txt", uploaded_by=self.user) for i in range(102)]
        )
        response = self.client.get("/api/documents/status/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertTrue(data["documents_complete"])
        self.assertEqual(len(data["documents"]), 102)
        self.assertEqual(len(data["dashboard_document_ids"]), 50)
        self.assertEqual(data["stats"]["total_docs_count"], 102)

    def test_reembedding_claim_rejects_stale_cancelled_and_duplicate_deliveries(self):
        doc = SourceDocument.objects.create(original_filename="edit.txt", cloud_task_name="current")
        token = surreal_db.document_task_name.set("stale")
        try:
            self.assertIsNone(surreal_db.claim_document_for_reembedding(str(doc.uuid)))
            surreal_db.document_task_name.set("current")
            doc.cancel_requested = True
            doc.save()
            self.assertIsNone(surreal_db.claim_document_for_reembedding(str(doc.uuid)))
            doc.cancel_requested = False
            doc.save()
            self.assertEqual(surreal_db.claim_document_for_reembedding(str(doc.uuid))["status"], "EMBEDDING")
            self.assertIsNone(surreal_db.claim_document_for_reembedding(str(doc.uuid)))
        finally:
            surreal_db.document_task_name.reset(token)

    def test_reembedding_claim_allows_legacy_delivery_without_task_name(self):
        doc = SourceDocument.objects.create(original_filename="legacy-edit.txt", status="COMPLETED")
        self.assertEqual(surreal_db.claim_document_for_reembedding(str(doc.uuid))["status"], "EMBEDDING")

    def test_reembedding_claim_allows_completed_document_with_current_task_identity(self):
        doc = SourceDocument.objects.create(
            original_filename="queued-edit.txt", status="COMPLETED", cloud_task_name="current"
        )
        token = surreal_db.document_task_name.set("current")
        try:
            claimed = surreal_db.claim_document_for_reembedding(str(doc.uuid))
        finally:
            surreal_db.document_task_name.reset(token)

        self.assertEqual(claimed["status"], "EMBEDDING")

    @override_settings(SURREALDB_OFFLINE=True)
    def test_offline_worker_update_rechecks_task_fence_under_lock(self):
        doc = SourceDocument.objects.create(
            original_filename="fenced.txt", status="EXTRACTING", cloud_task_name="current"
        )
        token = surreal_db.document_task_name.set("stale")
        try:
            self.assertEqual(surreal_db.update_document(str(doc.uuid), {"status": "REFINING"}), {})
        finally:
            surreal_db.document_task_name.reset(token)

        doc.refresh_from_db()
        self.assertEqual(doc.status, "EXTRACTING")

    @override_settings(SURREALDB_OFFLINE=True)
    def test_offline_worker_update_skips_cancelled_legacy_delivery(self):
        doc = SourceDocument.objects.create(
            original_filename="cancelled-legacy.txt", status="EXTRACTING", cancel_requested=True
        )

        self.assertEqual(surreal_db.update_document(str(doc.uuid), {"status": "REFINING"}), {})

        doc.refresh_from_db()
        self.assertEqual(doc.status, "EXTRACTING")

    @override_settings(SURREALDB_OFFLINE=True)
    def test_stage1_skips_save_and_broadcast_after_worker_is_cancelled(self):
        from extractor import tasks

        doc = SourceDocument.objects.create(
            original_filename="cancelled.txt", status="EXTRACTING", cloud_task_name="current", cancel_requested=True
        )
        token = surreal_db.document_task_name.set("current")
        try:
            with (
                patch.object(
                    tasks,
                    "_get_doc_info_stage1",
                    return_value=(surreal_db.get_document(str(doc.uuid)), "txt", str(doc.uuid)),
                ),
                patch.object(
                    tasks,
                    "_acquire_stage1_raw_markdown",
                    return_value=("content", "TEXT", 1, Decimal("0"), 0, 0),
                ),
                patch.object(tasks, "broadcast_status_change") as broadcast,
            ):
                tasks._run_stage1("unused", str(doc.uuid))
        finally:
            surreal_db.document_task_name.reset(token)

        doc.refresh_from_db()
        self.assertEqual(doc.status, "EXTRACTING")
        broadcast.assert_not_called()

    @override_settings(SURREALDB_OFFLINE=True)
    def test_stage1_skips_cancelled_legacy_delivery_without_task_name(self):
        from extractor import tasks

        doc = SourceDocument.objects.create(
            original_filename="cancelled-legacy.txt", status="EXTRACTING", cancel_requested=True
        )
        with (
            patch.object(
                tasks,
                "_get_doc_info_stage1",
                return_value=(surreal_db.get_document(str(doc.uuid)), "txt", str(doc.uuid)),
            ),
            patch.object(
                tasks, "_acquire_stage1_raw_markdown", return_value=("content", "TEXT", 1, Decimal("0"), 0, 0)
            ),
            patch.object(tasks, "broadcast_status_change") as broadcast,
        ):
            tasks._run_stage1("unused", str(doc.uuid))

        doc.refresh_from_db()
        self.assertEqual(doc.status, "EXTRACTING")
        broadcast.assert_not_called()

    @override_settings(SURREALDB_OFFLINE=True)
    def test_private_hash_deduplication_never_reads_another_users_document(self):
        other = User.objects.create_user(username="other-tenant", password="test-password")
        SourceDocument.objects.create(
            original_filename="private.txt",
            file_hash="same-private-hash",
            status="COMPLETED",
            uploaded_by=other,
        )
        self.assertIsNone(_find_existing_doc_by_hash("same-private-hash", str(self.user.id)))

    @patch("extractor.views.DocumentDeleteView._purge_physical_file", side_effect=RuntimeError("Storage unavailable"))
    def test_failed_file_delete_retains_document(self, _purge):
        doc = SourceDocument.objects.create(original_filename="keep.txt", status="COMPLETED", uploaded_by=self.user)
        response = self.client.post(reverse("delete_document", args=[doc.uuid]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Storage unavailable", response.json()["error"])
        self.assertTrue(SourceDocument.objects.filter(pk=doc.pk).exists())

    @patch("extractor.cloud_tasks.cancel_document_task", side_effect=RuntimeError("Queue endpoint unavailable"))
    def test_cancel_error_does_not_expose_provider_detail(self, _cancel):
        doc = SourceDocument.objects.create(original_filename="cancel.txt", status="PENDING", uploaded_by=self.user)
        response = self.client.post(reverse("cancel_document", args=[doc.uuid]), HTTP_X_REQUESTED_WITH="XMLHttpRequest")
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Queue endpoint unavailable", response.json()["error"])

    @patch("extractor.deployment.update_service_scale")
    def test_staff_cannot_change_scaling(self, scale):
        self.user.is_superuser = False
        self.user.save(update_fields=["is_superuser"])
        response = self.client.post(reverse("deployment_controller"), {"mode": "hibernate"})
        self.assertEqual(response.status_code, 403)
        scale.assert_not_called()

    @override_settings(WORKER_SERVICE_NAME="configured-worker")
    @patch("extractor.deployment.update_service_scale")
    @patch("extractor.deployment.get_service_config", side_effect=RuntimeError("Worker unavailable"))
    def test_worker_lookup_failure_never_scales_web(self, config, scale):
        response = self.client.post(reverse("deployment_controller"), {"mode": "hibernate"})
        self.assertEqual(response.status_code, 302)
        config.assert_called_once_with("configured-worker")
        scale.assert_not_called()

    @override_settings(SURREALDB_OFFLINE=False)
    @patch(
        "extractor.deployment.check_service_dependencies_health", return_value={"surrealdb": False, "supabase": True}
    )
    @patch("extractor.deployment.get_service_config", return_value={"spec": {}})
    @patch("extractor.deployment.update_service_scale")
    @patch("extractor.surreal_db.get_system_settings", return_value={})
    def test_deployment_controller_preflight_warns_when_surreal_offline(self, _settings, scale, _config, probe):
        response = self.client.post(reverse("deployment_controller"), {"mode": "on-demand"}, follow=True)
        self.assertEqual(response.status_code, 200)
        self.assertGreaterEqual(probe.call_count, 1)
        scale.assert_called_once()
        messages = list(response.context["messages"])
        self.assertTrue(any("Preflight alert" in str(m) for m in messages))

    @override_settings(DEBUG=False)
    @patch("extractor.cloud_tasks._get_tasks_client")
    def test_cancel_persists_marker_and_deletes_queue_task(self, client):
        doc = SourceDocument.objects.create(
            original_filename="cancel.txt", status="PENDING", cloud_task_name="projects/test/tasks/task-1"
        )
        cloud_tasks.cancel_document_task(str(doc.uuid), surreal_db.get_document(str(doc.uuid)))
        doc.refresh_from_db()
        self.assertTrue(doc.cancel_requested)
        self.assertEqual(doc.status, "FAILED")
        self.assertIn("stopped", doc.error_message)
        client.return_value.delete_task.assert_called_once_with(name=doc.cloud_task_name)

    @override_settings(DEBUG=False)
    @patch("extractor.cloud_tasks._get_tasks_client")
    def test_queue_cleanup_failure_remains_retryable(self, client):
        doc = SourceDocument.objects.create(original_filename="cancel.txt", cloud_task_name="task-1")
        client.return_value.delete_task.side_effect = RuntimeError("Queue unavailable")
        with self.assertRaisesRegex(RuntimeError, "queue cleanup failed"):
            cloud_tasks.cancel_document_task(str(doc.uuid), surreal_db.get_document(str(doc.uuid)))
        doc.refresh_from_db()
        self.assertTrue(doc.cancel_requested)
        self.assertEqual(doc.cloud_task_name, "task-1")
        client.return_value.delete_task.side_effect = NotFound("Already gone")
        cloud_tasks.cancel_document_task(str(doc.uuid), surreal_db.get_document(str(doc.uuid)))

    @patch("django.core.cache.cache.add", side_effect=RuntimeError("Cache unreachable"))
    @patch("extractor.surreal_db.check_rate_limit_atomic", return_value=False)
    def test_rag_search_rate_limit_surreal_fallback_denies_on_limit(self, mock_atomic, _mock_cache):
        request = RequestFactory().get("/rag-search/?q=query")
        request.user = self.user
        response = DocumentRAGSearchView().get(request)
        self.assertEqual(response.status_code, 429)
        data = json.loads(response.content)
        self.assertIn("Search rate limit reached", data["error"])
        mock_atomic.assert_called_once()

    @patch("django.core.cache.cache.add", side_effect=RuntimeError("Cache unreachable"))
    @patch("extractor.surreal_db.check_rate_limit_atomic", return_value=False)
    def test_sft_preview_rate_limit_surreal_fallback_denies_on_limit(self, mock_atomic, _mock_cache):
        request = RequestFactory().get("/api/sft/preview/doc-123/")
        request.user = self.user
        response = SFTDatasetPreviewView().get(request, "doc-123")
        self.assertEqual(response.status_code, 429)
        data = json.loads(response.content)
        self.assertIn("Preview rate limit reached", data["message"])
        mock_atomic.assert_called_once()


class DurableDeliveryTests(SimpleTestCase):
    def test_aborted_final_stage_does_not_report_completion(self):
        from extractor import tasks

        with (
            patch.object(tasks, "_check_pipeline_active", return_value=True),
            patch.object(tasks, "check_budget_and_api_limit"),
            patch.object(tasks, "_run_stage1", return_value={}),
            patch.object(tasks, "_run_stage2", return_value={}),
            patch.object(tasks, "_run_stage3", return_value=None),
            patch.object(tasks, "log_audit_event") as audit,
            patch.object(tasks, "_handle_stage_failure") as failure,
        ):
            self.assertFalse(tasks._run_pipeline_stages({}, "/unused", "doc-1"))
        audit.assert_not_called()
        failure.assert_not_called()

    @override_settings(DEBUG=False, WORKER_URL="", APP_URL="https://web.example.test")
    @patch("extractor.cloud_tasks.get_gcp_project_details", return_value={"project_id": "test"})
    def test_production_never_routes_tasks_to_web(self, _details):
        with self.assertRaisesRegex(RuntimeError, "worker URL"):
            cloud_tasks.enqueue("process_document", {"document_uuid": "doc-1"})

    @patch("extractor.surreal_db.get_document")
    def test_stale_duplicate_delivery_is_not_acknowledged(self, get_document):
        get_document.return_value = {
            "status": "EXTRACTING",
            "cloud_task_name": "task-1",
            "updated_at": (timezone.now() - timedelta(minutes=6)).isoformat(),
        }
        with self.assertRaisesRegex(RuntimeError, "terminal outcome"):
            task_handlers._confirm_document_outcome(
                "process_document", {"document_uuid": "doc-1", "cloud_task_name": "task-1"}
            )

    @patch("extractor.surreal_db.get_document")
    def test_active_duplicate_delivery_can_be_acknowledged(self, get_document):
        get_document.return_value = {
            "status": "EXTRACTING",
            "cloud_task_name": "task-1",
            "updated_at": (timezone.now() - timedelta(minutes=1)).isoformat(),
        }
        task_handlers._confirm_document_outcome(
            "process_document", {"document_uuid": "doc-1", "cloud_task_name": "task-1"}
        )

    @patch("extractor.surreal_db.get_document", return_value={"status": "PENDING", "cloud_task_name": "task-2"})
    def test_superseded_delivery_can_be_acknowledged(self, _get):
        task_handlers._confirm_document_outcome(
            "process_document", {"document_uuid": "doc-1", "cloud_task_name": "task-1"}
        )

    @override_settings(SURREALDB_OFFLINE=False)
    @patch("extractor.surreal_db._first_result", return_value=[])
    @patch("extractor.surreal_db._run")
    def test_worker_writes_are_fenced_by_identity_and_cancellation(self, run, _result):
        token = surreal_db.document_task_name.set("task-1")
        try:
            surreal_db.update_document("doc-1", {"status": "COMPLETED", "error_message": "detail"})
        finally:
            surreal_db.document_task_name.reset(token)
        sql, params = run.call_args.args
        self.assertIn("cloud_task_name = $expected_task", sql)
        self.assertIn("cancel_requested != true", sql)
        self.assertEqual(params["error_message"], "detail")
        self.assertEqual(params["expected_task"], "task-1")

    @patch("django.core.files.storage.default_storage.exists", side_effect=OSError("Storage down"))
    def test_storage_errors_propagate_to_delete_view(self, _exists):
        with self.assertRaisesRegex(RuntimeError, "retained"):
            DocumentDeleteView._purge_physical_file("stored.txt", "hash", 0)

    @override_settings(DEBUG=True)
    def test_task_payload_must_be_an_object(self):
        from django.test import RequestFactory

        with patch.dict(task_handlers.TASK_REGISTRY, {"test_payload": MagicMock()}):
            request = RequestFactory().post("/internal/tasks/test_payload/", data="[]", content_type="application/json")
            response = task_handlers.CloudTaskHandlerView().post(request, "test_payload")
        self.assertEqual(response.status_code, 400)
        self.assertIn("JSON object", json.loads(response.content)["error"])

    @patch("extractor.cloud_tasks.cancel_document_task")
    def test_bulk_delete_cancels_inflight_tasks(self, mock_cancel):
        from extractor.views import _abort_inflight_processing

        mock_doc = MagicMock()
        mock_doc.status = "EXTRACTING"
        mock_surreal = MagicMock()
        mock_surreal.get_document.return_value = {"status": "EXTRACTING", "cloud_task_name": "task-abc"}

        with patch("extractor.views.broadcast_status_change") as mock_broadcast:
            _abort_inflight_processing(mock_doc, "doc-uuid-999", mock_surreal)
            mock_cancel.assert_called_once_with("doc-uuid-999", {"status": "EXTRACTING", "cloud_task_name": "task-abc"})
            mock_broadcast.assert_called_once_with("doc-uuid-999", "FAILED")

    @patch("extractor.cloud_tasks.enqueue")
    @patch("extractor.surreal_db.update_document")
    def test_reaper_clears_cancel_requested_flag(self, mock_update, mock_enqueue):
        from extractor.task_state import CANCEL_REQUESTED
        from extractor.tasks import _reap_single_stale_doc

        doc = {
            "id": "doc:123",
            "doc_uuid": "test-uuid-reap",
            "status": "EXTRACTING",
            "retry_count": 1,
        }
        with patch("extractor.utils.broadcast_status_change"):
            success = _reap_single_stale_doc(doc)
            self.assertTrue(success)
            mock_update.assert_called_once()
            called_uuid, payload = mock_update.call_args[0]
            self.assertEqual(called_uuid, "test-uuid-reap")
            self.assertEqual(payload["status"], "PENDING")
            self.assertEqual(payload["retry_count"], 2)
            self.assertFalse(payload[CANCEL_REQUESTED])

    @patch("extractor.llm_gateway._init_refinement_client")
    @patch("extractor.llm_gateway.execute_generate_content_with_fallback")
    @patch("extractor.rag.generate_surreal_embeddings")
    @patch("extractor.surreal_db.add_user_memory")
    def test_store_user_memory_uses_standard_model_constant(
        self, mock_add_memory, mock_embeddings, mock_gen, mock_init_client
    ):
        from extractor.llm_gateway import MODEL_GEMINI_FLASH_LITE
        from extractor.tasks import store_user_memory_task

        mock_gen_response = MagicMock()
        mock_gen_response.text = "User prefers detailed summaries."
        mock_gen.return_value = (mock_gen_response, 50)
        mock_embeddings.return_value = [[0.1] * 768]

        store_user_memory_task({"user_id": "user-uuid-1", "text": "Please provide detailed responses."})

        mock_gen.assert_called_once()
        called_args = mock_gen.call_args[0]
        # called_args: (client, model, contents=...)
        self.assertEqual(called_args[1], MODEL_GEMINI_FLASH_LITE)
