import os
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse

from extractor.models import SourceDocument


class ReembeddingTestCase(TestCase):
    """Verifies that manually edited documents can be successfully re-chunked and re-embedded in the background."""

    @patch("extractor.tasks.generate_surreal_embeddings")
    @patch("extractor.surreal_db.recreate_chunks")
    def test_reembed_edited_document_task(self, mock_recreate, mock_embeddings):
        # Create a document in COMPLETED state
        doc = SourceDocument.objects.create(
            original_filename="sample.txt",
            file_hash="sample-hash",
            title="Old Title",
            status="COMPLETED",
            refined_markdown="This is paragraph one.\n\nThis is paragraph two.",
        )

        # Now edit document markdown
        doc.refined_markdown = (
            "New manually edited paragraph.\n\nAnother manually edited paragraph.\n\nAnd a third paragraph."
        )
        doc.save()

        # Mock the embedding generator to return one synthetic embedding
        mock_embeddings.return_value = [[0.1] * 768]

        # Trigger re-embedding task directly
        from extractor.tasks import reembed_edited_document_task

        reembed_edited_document_task({"document_id": doc.id})

        # Refresh document
        doc.refresh_from_db()
        self.assertEqual(doc.status, "COMPLETED")
        self.assertEqual(doc.page_count, 1)

        # Assert recreate_chunks is successfully called
        mock_recreate.assert_called_once()


class ResilienceAndSafetyTestCase(TestCase):
    """
    Verifies that the curation pipeline has robust error resilience, fallbacks for shared-db GCS
    storage, mid-pipeline budget cap circuit breakers, stale task reapers, and manual retry triggers.
    """

    def test_get_working_path_local_missing_fallback(self):
        from extractor.tasks import _get_working_path

        doc = SourceDocument.objects.create(
            original_filename="staged_booklet.pdf",
            file_hash="mock-staged-hash",
            title="Staged Booklet",
            status="PENDING",
        )
        # Mock file field to simulate file missing on local disk but readable via .read()
        doc.file = SimpleUploadedFile("staged_booklet.pdf", b"Simulated PDF content stream")
        doc.save()

        # Override doc.file to point to a non-existent path on local disk
        # (simulating GCS production files loaded on a local workspace)
        import io

        mock_file = MagicMock()
        mock_file.path = "/nonexistent/path/on/local/disk/booklet.pdf"
        sim_stream = io.BytesIO(b"Simulated PDF content stream")
        mock_file.read.side_effect = sim_stream.read
        mock_file.name = "booklet.pdf"

        with patch.object(doc, "file", mock_file):
            working_path, temp_local_path = _get_working_path(doc)

            self.assertIsNotNone(temp_local_path)
            self.assertTrue(os.path.exists(working_path))
            with open(working_path, "rb") as f:
                content = f.read()
            self.assertEqual(content, b"Simulated PDF content stream")

            # Clean up temporary file
            if temp_local_path and os.path.exists(temp_local_path):
                os.unlink(temp_local_path)

    @patch("django.utils.timezone.now")
    def test_reap_stale_tasks(self, mock_now):
        from datetime import datetime, timedelta

        import pytz

        from extractor.tasks import reap_stale_tasks

        # Set base time to 2026-06-19 12:00:00 UTC
        base_time = datetime(2026, 6, 19, 12, 0, 0, tzinfo=pytz.UTC)
        mock_now.return_value = base_time

        # 1. Stuck task (> 15 minutes ago)
        doc_stale = SourceDocument.objects.create(
            original_filename="stuck_booklet.pdf", file_hash="stale-hash", status="EXTRACTING"
        )
        SourceDocument.objects.filter(id=doc_stale.id).update(updated_at=base_time - timedelta(minutes=16))

        # 2. Healthy active task (< 15 minutes ago)
        doc_healthy = SourceDocument.objects.create(
            original_filename="healthy_booklet.pdf", file_hash="healthy-hash", status="EXTRACTING"
        )
        SourceDocument.objects.filter(id=doc_healthy.id).update(updated_at=base_time - timedelta(minutes=5))

        # Run stale task reaper
        reaped_count = reap_stale_tasks()

        # Verify stuck task reaped and healthy task unaffected
        doc_stale_refreshed = SourceDocument.objects.get(id=doc_stale.id)
        doc_healthy_refreshed = SourceDocument.objects.get(id=doc_healthy.id)

        self.assertEqual(reaped_count, 1)
        self.assertEqual(doc_stale_refreshed.status, "FAILED")
        self.assertIn("Task terminated unexpectedly", doc_stale_refreshed.error_message)
        self.assertEqual(doc_healthy_refreshed.status, "EXTRACTING")

    @patch("extractor.cloud_tasks.enqueue")
    def test_document_retry_view(self, mock_enqueue):
        _user = User.objects.create_user(username="retrytestuser", password="password123")
        self.client.login(username="retrytestuser", password="password123")

        doc = SourceDocument.objects.create(
            original_filename="failed_booklet.pdf",
            file_hash="failed-hash",
            status="FAILED",
            cost_usd=Decimal("0.18"),
            input_tokens=1500,
            output_tokens=750,
            error_message="Stage 2 Failure: rate limit reached",
            uploaded_by=_user,
            retry_count=0,
        )

        retry_url = reverse("retry_document", kwargs={"doc_uuid": doc.uuid})

        # Simulate standard post request
        response = self.client.post(retry_url)

        # Verify redirection to dashboard, state reset, retry_count increment, and queue re-dispatch
        self.assertEqual(response.status_code, 302)
        doc_refreshed = SourceDocument.objects.get(id=doc.id)
        self.assertEqual(doc_refreshed.status, "PENDING")
        self.assertEqual(doc_refreshed.cost_usd, Decimal("0.00"))
        self.assertEqual(doc_refreshed.input_tokens, 0)
        self.assertEqual(doc_refreshed.output_tokens, 0)
        self.assertEqual(doc_refreshed.error_message, "")
        self.assertEqual(doc_refreshed.retry_count, 1)

        mock_enqueue.assert_called_once_with("process_document", {"document_id": doc.id})

    @patch("extractor.cloud_tasks.enqueue")
    def test_document_retry_limit_exceeded(self, mock_enqueue):
        _user = User.objects.create_user(username="retrytestuser2", password="password123")
        self.client.login(username="retrytestuser2", password="password123")

        doc = SourceDocument.objects.create(
            original_filename="failed_booklet.pdf",
            file_hash="failed-hash",
            status="FAILED",
            cost_usd=Decimal("0.18"),
            input_tokens=1500,
            output_tokens=750,
            error_message="Stage 2 Failure: rate limit reached",
            uploaded_by=_user,
            retry_count=3,
        )

        retry_url = reverse("retry_document", kwargs={"doc_uuid": doc.uuid})

        response = self.client.post(retry_url)
        self.assertEqual(response.status_code, 302)  # redirects to dashboard with error message

        doc_refreshed = SourceDocument.objects.get(id=doc.id)
        # Should NOT reset or enqueue
        self.assertEqual(doc_refreshed.status, "FAILED")
        self.assertEqual(doc_refreshed.retry_count, 3)
        mock_enqueue.assert_not_called()

    @patch("extractor.tasks.check_budget_and_api_limit")
    @patch("extractor.tasks._run_stage1")
    @patch("extractor.tasks._get_working_path")
    @patch("extractor.tasks._prepare_document_for_processing")
    @patch("extractor.tasks._run_stage2")
    def test_mid_pipeline_budget_circuit_breaker(self, mock_stage2, mock_prep, mock_working, mock_stage1, mock_check):
        from extractor.tasks import process_document_task

        doc = SourceDocument.objects.create(
            original_filename="breaker_booklet.pdf", file_hash="breaker-hash", status="PENDING"
        )

        mock_prep.return_value = doc
        mock_working.return_value = ("/tmp/breaker_booklet.pdf", None)  # nosec B108
        mock_stage1.return_value = doc

        # Simulate budget check failure during mid-pipeline check before Stage 2
        mock_check.side_effect = Exception("Monthly USD budget cap exceeded!")

        process_document_task({"document_id": doc.id})

        # Verify that Stage 2 was aborted, and status set to FAILED with mid-pipeline budget warning
        mock_stage2.assert_not_called()
        doc_refreshed = SourceDocument.objects.get(id=doc.id)
        self.assertEqual(doc_refreshed.status, "FAILED")
        self.assertIn("Mid-Pipeline Budget Capped Halt", doc_refreshed.error_message)
