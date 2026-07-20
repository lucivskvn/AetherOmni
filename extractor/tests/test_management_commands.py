import datetime
from io import StringIO
from unittest.mock import patch
from django.test import TestCase
from django.core.management import call_command
from extractor.models import SourceDocument

class BackfillMetadataTestCase(TestCase):
    def setUp(self):
        cutoff_date = datetime.datetime(2026, 7, 15, tzinfo=datetime.UTC)
        self.doc_needing_backfill = SourceDocument.objects.create(
            original_filename="test1.pdf",
            status="COMPLETED",
            publisher="Unknown",
            doi="",
        )
        SourceDocument.objects.filter(id=self.doc_needing_backfill.id).update(created_at=cutoff_date)

        self.doc_complete = SourceDocument.objects.create(
            original_filename="test2.pdf",
            status="COMPLETED",
            publisher="Academic Press",
            doi="10.1000/182",
        )
        SourceDocument.objects.filter(id=self.doc_complete.id).update(created_at=cutoff_date)

    @patch("extractor.management.commands.backfill_metadata.enqueue")
    def test_backfill_metadata_command(self, mock_enqueue):
        out = StringIO()
        call_command("backfill_metadata", stdout=out)
        output = out.getvalue()
        self.assertIn("Successfully re-queued 1 documents", output)
        mock_enqueue.assert_called_once()

    @patch("extractor.management.commands.backfill_metadata.enqueue")
    def test_backfill_metadata_no_documents(self, mock_enqueue):
        SourceDocument.objects.all().delete()
        out = StringIO()
        call_command("backfill_metadata", stdout=out)
        output = out.getvalue()
        self.assertIn("No documents require backfilling!", output)
        mock_enqueue.assert_not_called()
