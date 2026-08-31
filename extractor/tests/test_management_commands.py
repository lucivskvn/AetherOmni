import datetime
from io import StringIO
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

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


class InitSurrealAndMigratorTestCase(TestCase):
    def test_create_local_superuser_stub_unusable_password(self):
        import scripts.init_surreal as init_surreal

        admin_email = "bootstrap-admin@example.com"
        user = init_surreal._create_local_superuser_stub(admin_email)
        self.assertTrue(user.is_staff)
        self.assertTrue(user.is_superuser)
        self.assertFalse(user.has_usable_password())

    def test_namespace_migrator_url_scheme_conversion(self):
        from scripts.migrate_surreal_namespace import SurrealNamespaceMigrator

        migrator_ws = SurrealNamespaceMigrator(
            surreal_url="ws://127.0.0.1:8000/rpc",
            user="root",
            password="rootpassword",
        )
        self.assertEqual(migrator_ws.surreal_url, "http://127.0.0.1:8000")

        migrator_wss = SurrealNamespaceMigrator(
            surreal_url="wss://surreal.example.com/rpc/",
            user="root",
            password="rootpassword",
        )
        self.assertEqual(migrator_wss.surreal_url, "https://surreal.example.com")

    def test_extractor_config_shutdown_event(self):
        from extractor.apps import ExtractorConfig

        # Verify ExtractorConfig has a valid shutdown Event
        self.assertFalse(ExtractorConfig._shutdown_event.is_set())
