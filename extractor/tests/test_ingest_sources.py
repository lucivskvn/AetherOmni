import os
import tempfile
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from extractor.models import SourceDocument


class IngestSourcesTestCase(TestCase):
    """Direct unit tests for ingest_sources management command."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.sources_dir = self.temp_dir.name

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_sources_dir_not_exists(self):
        with self.assertRaises(CommandError):
            call_command("ingest_sources", sources_dir="/non-existent-dir-path")

    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.management.commands.ingest_sources.check_budget_and_api_limit")
    @patch("extractor.management.commands.ingest_sources.cloud_tasks.enqueue")
    def test_ingest_fresh_file_async(self, mock_enqueue, mock_check_budget, mock_hash):
        mock_hash.return_value = "dummyhash123"
        # Create a temp booklet file
        file_path = os.path.join(self.sources_dir, "booklet.pdf")
        with open(file_path, "wb") as f:
            f.write(b"Dummy PDF Content")

        call_command("ingest_sources", sources_dir=self.sources_dir, sync=False)

        # A document record should be created with PENDING status
        doc = SourceDocument.objects.filter(file_hash="dummyhash123").first()
        self.assertIsNotNone(doc)
        self.assertEqual(doc.status, "PENDING")
        self.assertEqual(doc.original_filename, "booklet.pdf")

    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.management.commands.ingest_sources.check_budget_and_api_limit")
    @patch("extractor.management.commands.ingest_sources.process_document_task")
    def test_ingest_fresh_file_sync(self, mock_process_task, mock_check_budget, mock_hash):
        mock_hash.return_value = "dummyhash456"
        file_path = os.path.join(self.sources_dir, "booklet.txt")
        with open(file_path, "wb") as f:
            f.write(b"Dummy Text Content")

        call_command("ingest_sources", sources_dir=self.sources_dir, sync=True)

        doc = SourceDocument.objects.filter(file_hash="dummyhash456").first()
        self.assertIsNotNone(doc)
        mock_process_task.assert_called_once_with({"document_id": doc.id})

    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.surreal_db.clone_chunks")
    def test_ingest_duplicate_file(self, mock_clone_chunks, mock_hash):
        mock_hash.return_value = "duplicatehash"

        # Pre-create completed document in DB
        existing_doc = SourceDocument.objects.create(
            original_filename="first.pdf",
            file_hash="duplicatehash",
            status="COMPLETED",
            language="Arabic",
            author="Scholar A",
            title="Book Title",
            document_type="PDF",
            page_count=10,
            cost_usd=Decimal("0.02"),
        )

        file_path = os.path.join(self.sources_dir, "second.pdf")
        with open(file_path, "wb") as f:
            f.write(b"Same Content")

        call_command("ingest_sources", sources_dir=self.sources_dir, sync=False)

        # A new completed document record should be cloned
        new_doc = SourceDocument.objects.filter(original_filename="second.pdf").first()
        self.assertIsNotNone(new_doc)
        self.assertEqual(new_doc.status, "COMPLETED")
        self.assertEqual(new_doc.cost_usd, Decimal("0.00"))
        mock_clone_chunks.assert_called_once_with(str(existing_doc.uuid), str(new_doc.uuid))
