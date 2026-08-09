"""Unit tests for backfill_metadata management command."""

import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from extractor.models import SourceDocument


class BackfillMetadataCommandTest(TestCase):
    def setUp(self):
        cutoff = datetime.datetime(2026, 7, 10, tzinfo=datetime.UTC)
        self.doc_needing_backfill = SourceDocument.objects.create(
            original_filename="old_doc.pdf",
            file_hash="hash1",
            title="Old Doc",
            status="COMPLETED",
            publisher="Unknown",
            doi="",
        )
        SourceDocument.objects.filter(id=self.doc_needing_backfill.id).update(created_at=cutoff)

        self.doc_complete = SourceDocument.objects.create(
            original_filename="new_doc.pdf",
            file_hash="hash2",
            title="New Doc",
            status="COMPLETED",
            publisher="Nature",
            doi="10.1038/s41586-026-0000-0",
        )

    @patch("extractor.management.commands.backfill_metadata.enqueue")
    def test_backfill_metadata_command(self, mock_enqueue):
        out = StringIO()
        call_command("backfill_metadata", stdout=out)

        self.doc_needing_backfill.refresh_from_db()
        self.assertEqual(self.doc_needing_backfill.status, "PENDING")
        mock_enqueue.assert_called_once()
        self.assertIn("Re-queued document", out.getvalue())

    @patch("extractor.management.commands.backfill_metadata.enqueue")
    def test_backfill_metadata_no_documents(self, mock_enqueue):
        SourceDocument.objects.all().delete()
        out = StringIO()
        call_command("backfill_metadata", stdout=out)
        self.assertIn("No documents require backfilling!", out.getvalue())
        mock_enqueue.assert_not_called()
