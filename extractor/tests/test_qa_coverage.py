import os
import tempfile
from unittest.mock import patch

from django.core.management import CommandError, call_command
from django.test import RequestFactory, TestCase

from extractor.context_processors import system_settings
from extractor.models import SourceDocument, SystemSettings


class ContextProcessorsTestCase(TestCase):
    """Verifies that the System Settings context processor operates correctly."""

    def test_system_settings_context_processor(self):
        factory = RequestFactory()
        request = factory.get("/")

        # Ensure SystemSettings object is created
        settings_obj = SystemSettings.get_settings()

        context = system_settings(request)
        self.assertIn("system_settings", context)
        self.assertEqual(context["system_settings"], settings_obj)
        self.assertIn("SUPABASE_URL", context)
        self.assertIn("SUPABASE_PUBLIC_KEY", context)


class IngestSourcesCommandTestCase(TestCase):
    """Verifies all branches of the booklet ingestion management command."""

    def test_sources_dir_does_not_exist(self):
        with self.assertRaises(CommandError):
            call_command("ingest_sources", sources_dir="/nonexistent/dir/path")

    def test_no_files_found(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            # Empty directory
            call_command("ingest_sources", sources_dir=temp_dir)
            # The command should exit cleanly without creating documents

    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.management.commands.ingest_sources.process_document_task")
    @patch("extractor.management.commands.ingest_sources.cloud_tasks.enqueue")
    def test_fresh_ingestion_sync(self, mock_enqueue, mock_process_task, mock_sha256):
        mock_sha256.return_value = "dummy-hash-123"

        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a mock text file
            file_path = os.path.join(temp_dir, "test_booklet.txt")
            with open(file_path, "w") as f:
                f.write("Some test booklet content")

            # Run command synchronously
            call_command("ingest_sources", sources_dir=temp_dir, sync=True)

            # Verify document was created
            doc = SourceDocument.objects.get(file_hash="dummy-hash-123")
            self.assertEqual(doc.original_filename, "test_booklet.txt")
            self.assertEqual(doc.status, "PENDING")

            # Verify process_document_task was called with doc id
            mock_process_task.assert_called_once_with({"document_id": doc.id})

    @patch("django.db.transaction.on_commit", lambda f: f())
    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.management.commands.ingest_sources.cloud_tasks.enqueue")
    def test_fresh_ingestion_async(self, mock_enqueue, mock_sha256):
        mock_sha256.return_value = "dummy-hash-456"

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, "test_booklet_async.txt")
            with open(file_path, "w") as f:
                f.write("Some test booklet content async")

            # Run command asynchronously (default)
            call_command("ingest_sources", sources_dir=temp_dir)

            # Verify document was created
            doc = SourceDocument.objects.get(file_hash="dummy-hash-456")
            self.assertEqual(doc.original_filename, "test_booklet_async.txt")

            # Verify cloud_tasks.enqueue was called to dispatch
            mock_enqueue.assert_called_once_with("process_document", {"document_id": doc.id})

    @patch("extractor.management.commands.ingest_sources.calculate_file_sha256")
    @patch("extractor.surreal_db.clone_chunks")
    def test_duplicate_ingestion(self, mock_clone_chunks, mock_sha256):
        mock_sha256.return_value = "duplicate-hash-789"

        # Pre-create an existing document that is completed
        existing_doc = SourceDocument.objects.create(
            original_filename="original.txt",
            file_hash="duplicate-hash-789",
            status="COMPLETED",
            title="Original Book",
            cost_usd=0.01,
            language="en",
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            file_path = os.path.join(temp_dir, "duplicate_booklet.txt")
            with open(file_path, "w") as f:
                f.write("Same booklet content")

            # Run command
            call_command("ingest_sources", sources_dir=temp_dir)

            # Verify a new document was created with status COMPLETED (instant cache)
            new_doc = SourceDocument.objects.get(original_filename="duplicate_booklet.txt")
            self.assertEqual(new_doc.status, "COMPLETED")
            self.assertEqual(new_doc.title, "Original Book (Duplicate Source)")

            # Verify chunks cloned in SurrealDB
            mock_clone_chunks.assert_called_once_with(str(existing_doc.uuid), str(new_doc.uuid))
