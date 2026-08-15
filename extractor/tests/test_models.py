import os
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.test import RequestFactory, TestCase
from django.urls import reverse

from extractor.models import AuditLog, MonthlySpendLog, SafeVectorField, SourceDocument


class SafeVectorFieldTestCase(TestCase):
    """Verifies legacy SafeVectorField stub behaviour for migrations compatibility."""

    def test_vendor_returns_text(self):
        field = SafeVectorField()
        mock_connection = MagicMock()
        self.assertEqual(field.db_type(mock_connection), "TEXT")


class MonthlySpendLogTestCase(TestCase):
    @patch("extractor.surreal_db.kv_cache_get")
    def test_surreal_monthly_spend_reads_decoded_cache_values(self, mock_get):
        mock_get.return_value = {"accumulated_cost_usd": 12.345678}

        with self.settings(SURREALDB_OFFLINE=False):
            self.assertEqual(str(MonthlySpendLog.total_for_month(2026, 7)), "12.345678")


class AuditLogSignalsTestCase(TestCase):
    """Verifies that user login and logout signals trigger AuditLog creation."""

    def test_user_login_signal(self):
        user = User.objects.create_user(username="signaltestuser", password="password123")
        with self.settings(SUPABASE_URL="", SUPABASE_PUBLIC_KEY=""):
            self.client.login(username="signaltestuser", password="password123")

        logs = AuditLog.objects.filter(user=user, action="LOGIN")
        self.assertTrue(logs.exists())
        log = logs.first()
        self.assertEqual(log.details, "User 'signaltestuser' authenticated successfully.")

    def test_user_logout_signal(self):
        user = User.objects.create_user(username="signaltestuser", password="password123")
        with self.settings(SUPABASE_URL="", SUPABASE_PUBLIC_KEY=""):
            self.client.login(username="signaltestuser", password="password123")
            self.client.logout()

        logs = AuditLog.objects.filter(user=user, action="LOGOUT")
        self.assertTrue(logs.exists())
        log = logs.first()
        self.assertEqual(log.details, "User 'signaltestuser' logged out.")

    def test_user_login_signal_with_ip(self):
        user = User.objects.create_user(username="ipuser", password="password123")
        factory = RequestFactory()
        request = factory.get("/", HTTP_X_FORWARDED_FOR="198.51.100.1")

        from extractor.models import log_user_login

        log_user_login(sender=User, request=request, user=user)

        log = AuditLog.objects.filter(user=user, action="LOGIN").first()
        self.assertEqual(log.ip_address, "198.51.100.1")


class GdrpReferenceCountingTestCase(TestCase):
    """Verifies that physical file deletions follow GDPR rules and reference-counting constraints."""

    @patch("extractor.surreal_db.delete_chunks")
    def test_gdpr_file_deletion_reference_counting(self, mock_delete_chunks):
        # Create test user & login
        self.password = "T00rP@ssw0rd!"
        self.username = "gdpr.test"
        self.user = User.objects.create_user(username=self.username, password=self.password)
        self.client.force_login(self.user)

        file_hash = "gdpr-sample-hash-abc"

        # Mock a file storage backend for test case
        # First, upload Doc 1
        doc1 = SourceDocument.objects.create(
            original_filename="doc1.pdf",
            file_hash=file_hash,
            title="Booklet Alpha",
            status="COMPLETED",
            uploaded_by=self.user,
        )
        doc1.file.save("doc1.pdf", ContentFile(b"shared pdf contents"))

        # Save physical file path to verify preservation
        physical_path = doc1.file.path
        self.assertTrue(os.path.exists(physical_path))

        # Upload Doc 2 with matching file_hash (duplicate upload)
        doc2 = SourceDocument.objects.create(
            original_filename="doc2.pdf",
            file_hash=file_hash,
            title="Booklet Alpha (Copy)",
            status="COMPLETED",
            uploaded_by=self.user,
        )
        # Point to the same file path for storage replication
        doc2.file = doc1.file
        doc2.save()

        # Step 1: Delete Doc 1. The physical file MUST be preserved on disk!
        response = self.client.post(reverse("delete_document", args=[doc1.uuid]))
        self.assertEqual(response.status_code, 302)

        # Record should be gone
        self.assertFalse(SourceDocument.objects.filter(id=doc1.id).exists())
        # Other record must be preserved
        self.assertTrue(SourceDocument.objects.filter(id=doc2.id).exists())
        # PHYSICAL file must still be present!
        self.assertTrue(
            os.path.exists(physical_path), "Physical file was prematurely deleted while reference count is > 0!"
        )

        # Step 2: Delete Doc 2. The reference count drops to 0, physical file MUST be deleted!
        response = self.client.post(reverse("delete_document", args=[doc2.uuid]))
        self.assertEqual(response.status_code, 302)

        # Record should be gone
        self.assertFalse(SourceDocument.objects.filter(id=doc2.id).exists())
        # PHYSICAL file must be removed!
        self.assertFalse(
            os.path.exists(physical_path), "Physical file was not cleaned up after reference count dropped to 0!"
        )
