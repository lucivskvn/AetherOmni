from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from extractor.models import AuditAction, AuditLog, SourceDocument, SystemSettings


class ViewsTestCase(TestCase):
    """Verifies all Dashboard, Settings, Upload, Detail, Save, Delete, Purge, and Search Views."""

    def setUp(self):
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False

        self.user = User.objects.create_superuser(username="testviewuser", password="password123")
        self.client.login(username="testviewuser", password="password123")

        self.settings_obj = SystemSettings.get_settings()
        self.doc = SourceDocument.objects.create(
            original_filename="sample_test.txt",
            file_hash="mock-hash-abc-views",
            title="Sample Test Doc",
            status="COMPLETED",
            cost_usd=Decimal("0.05"),
            page_count=2,
            raw_markdown="Raw Content",
            refined_markdown="Refined Content",
        )

    def tearDown(self):
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False

    def test_dashboard_view_get(self):
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)
        self.assertIn("documents", response.context)
        self.assertIn("stats", response.context)

    def test_document_status_api_view_get(self):
        response = self.client.get(reverse("document_status_api"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("documents", data)
        self.assertIn("stats", data)
        self.assertEqual(len(data["documents"]), 1)
        self.assertEqual(data["documents"][0]["id"], self.doc.id)
        self.assertEqual(data["documents"][0]["status"], "COMPLETED")
        self.assertEqual(data["stats"]["COMPLETED"], 1)
        self.assertEqual(data["stats"]["total_docs_count"], 1)

    def test_save_settings_view_post_success(self):
        response = self.client.post(
            reverse("save_settings"),
            {
                "monthly_budget_usd": "25.50",
                "selected_model": "auto",
                "csrf_trusted_origins": "https://my-good-domain.com, http://my-second-domain.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_refreshed = SystemSettings.get_settings()
        self.assertEqual(settings_refreshed.monthly_budget_usd, Decimal("25.50"))
        self.assertEqual(
            settings_refreshed.csrf_trusted_origins, "https://my-good-domain.com, http://my-second-domain.com"
        )

    def test_save_settings_view_post_invalid_budget(self):
        response = self.client.post(
            reverse("save_settings"), {"monthly_budget_usd": "-10.00"}
        )
        self.assertEqual(response.status_code, 302)
        # Verify it didn't change to negative
        settings_refreshed = SystemSettings.get_settings()
        self.assertGreaterEqual(settings_refreshed.monthly_budget_usd, 0)

    def test_document_detail_view_get_success(self):
        response = self.client.get(reverse("document_detail", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["document"], self.doc)

    def test_document_detail_view_get_not_found(self):
        response = self.client.get(reverse("document_detail", args=["00000000-0000-0000-0000-000000000000"]))
        self.assertEqual(response.status_code, 404)

    def test_document_save_view_post_success(self):
        with patch("extractor.views.transaction.on_commit", lambda f: f()):
            with patch("extractor.cloud_tasks.enqueue") as mock_enqueue:
                response = self.client.post(
                    reverse("save_document", args=[self.doc.uuid]), {"refined_markdown": "### Highly Refined Markdown"}
                )
                self.assertEqual(response.status_code, 302)
                self.doc.refresh_from_db()
                self.assertEqual(self.doc.refined_markdown, "### Highly Refined Markdown")
                mock_enqueue.assert_called_once_with("reembed_document", {"document_id": self.doc.id})

    def test_document_delete_view_post_success(self):
        doc_to_delete = SourceDocument.objects.create(
            original_filename="delete_me.txt", file_hash="mock-hash-delete", title="Delete Me", status="COMPLETED"
        )
        response = self.client.post(reverse("delete_document", args=[doc_to_delete.uuid]))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SourceDocument.objects.filter(id=doc_to_delete.pk).count(), 0)

    def test_document_purge_all_view_post(self):
        response = self.client.post(reverse("purge_all_documents"))
        self.assertEqual(response.status_code, 302)
        self.assertEqual(SourceDocument.objects.count(), 0)

    @patch("extractor.views.query_semantic_knowledge_rag")
    def test_document_rag_search_view_success(self, mock_rag):
        mock_rag.return_value = {"answer": "This is a mock search answer.", "sources": []}
        response = self.client.get(reverse("rag_search") + "?q=test-query")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("answer_html", data)
        self.assertEqual(data["answer"], "This is a mock search answer.")

    def test_document_rag_search_view_empty_query(self):
        response = self.client.get(reverse("rag_search"))
        self.assertEqual(response.status_code, 400)

    def test_export_zip_view_empty(self):
        response = self.client.post(reverse("export_zip"))
        self.assertEqual(response.status_code, 302)

    @patch("extractor.views.generate_curated_zip_bundle")
    def test_export_zip_view_success(self, mock_zip):
        mock_zip.return_value = b"fake-zip-data"
        response = self.client.post(reverse("export_zip"), {"selected_documents": [self.doc.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response.content, b"fake-zip-data")

    def test_upload_view_post_empty(self):
        response = self.client.post(reverse("upload_document"))
        self.assertEqual(response.status_code, 302)

    @patch("extractor.views.calculate_file_sha256")
    @patch("extractor.cloud_tasks.enqueue")
    def test_upload_view_post_success(self, mock_enqueue, mock_sha256):
        mock_sha256.return_value = "mock-hash-fresh-upload"
        from django.core.files.uploadedfile import SimpleUploadedFile

        uploaded_file = SimpleUploadedFile("new_file.txt", b"plain text content")
        with patch("extractor.views.transaction.on_commit", lambda f: f()):
            response = self.client.post(reverse("upload_document"), {"file": uploaded_file})
            self.assertEqual(response.status_code, 302)
            new_doc = SourceDocument.objects.filter(file_hash="mock-hash-fresh-upload").first()
            self.assertIsNotNone(new_doc)
            self.assertEqual(new_doc.status, "PENDING")
            mock_enqueue.assert_called_once_with("process_document", {"document_id": new_doc.id})

    @patch("extractor.views.calculate_file_sha256")
    @patch("extractor.surreal_db.clone_chunks")
    def test_upload_view_post_duplicate_cache(self, mock_clone_chunks, mock_sha256):
        existing_doc = SourceDocument.objects.create(
            original_filename="existing.txt",
            file_hash="mock-hash-duplicate-cache",
            title="Existing Doc",
            status="COMPLETED",
            language="English",
            author="Author X",
            page_count=5,
        )

        mock_sha256.return_value = "mock-hash-duplicate-cache"
        from django.core.files.uploadedfile import SimpleUploadedFile

        uploaded_file = SimpleUploadedFile("duplicate.txt", b"plain text content")

        response = self.client.post(reverse("upload_document"), {"file": uploaded_file})
        self.assertEqual(response.status_code, 302)

        # Verify copy was created
        copies = SourceDocument.objects.filter(file_hash="mock-hash-duplicate-cache").exclude(id=existing_doc.id)
        self.assertEqual(copies.count(), 1)
        copy_doc = copies.first()
        self.assertEqual(copy_doc.status, "COMPLETED")
        self.assertEqual(copy_doc.title, "Existing Doc")

        # Verify chunks cloned
        mock_clone_chunks.assert_called_once_with(str(existing_doc.uuid), str(copy_doc.uuid))


class SecurityGatewayAndAuthTestCase(TestCase):
    """Verifies standard login redirection gating, session validation, and IDP integration."""

    def setUp(self):
        self.password = "T00rP@ssw0rd!"
        self.user_email = "scholar.test@example.com"
        self.username = "scholar.test"

        # Create standard test user
        self.user = User.objects.create_user(username=self.username, email=self.user_email, password=self.password)

        # Create a document for view endpoints testing
        self.doc = SourceDocument.objects.create(
            original_filename="security_gate.pdf",
            file_hash="security-hash-123",
            title="Secure Booklet",
            status="COMPLETED",
        )

    def test_anonymous_user_gated_and_redirected(self):
        """Verify anonymous requests to all operative views redirect to /login/."""
        gated_urls = [
            reverse("dashboard"),
            reverse("upload_document"),
            reverse("document_detail", args=[self.doc.uuid]),
            reverse("save_document", args=[self.doc.uuid]),
            reverse("delete_document", args=[self.doc.uuid]),
            reverse("purge_all_documents"),
            reverse("rag_search"),
            reverse("export_zip"),
            reverse("save_settings"),
            reverse("document_status_api"),
            reverse("retry_document", args=[self.doc.uuid]),
        ]

        for url in gated_urls:
            response = self.client.get(url)
            self.assertEqual(response.status_code, 302, f"URL {url} did not redirect!")
            self.assertTrue(response.url.startswith("/login"), f"URL {url} did not redirect to login gate!")

    def test_authenticated_user_access_allowed(self):
        """Verify that authenticated users can access the dashboard and views without redirect."""
        self.client.login(username=self.username, password=self.password)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_force_password_change_for_admin_password(self):
        """Verify that a user logged in with the default password 'admin' is forced to change it."""
        # Create a user with password 'admin'
        _default_user = User.objects.create_user(username="defaultadmin", password="admin")
        self.client.login(username="defaultadmin", password="admin")

        # Try to access the dashboard
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.startswith(reverse("password_change")))

        # Accessing password change view is allowed (returns 200)
        response = self.client.get(reverse("password_change"))
        self.assertEqual(response.status_code, 200)

    @patch("urllib.request.urlopen")
    def test_supabase_idp_auth_success(self, mock_urlopen):
        """Verify successful Supabase Auth response establishment and sync session backend."""
        import json

        from extractor.auth import SupabaseAuthBackend

        # Mock GoTrue login response JSON
        mock_response_data = {
            "user": {"id": "supabase-uuid-456", "email": "supabase.test@example.com"},
            "access_token": "mock-jwt-token-123",
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        # Execute Supabase auth flow with mock credentials
        backend = SupabaseAuthBackend()
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            user = backend.authenticate(None, username="supabase.test@example.com", password="somepassword")

        self.assertIsNotNone(user)
        self.assertEqual(user.email, "supabase.test@example.com")
        self.assertEqual(user.username, "supabase.test")

    @patch("urllib.request.urlopen")
    def test_supabase_idp_auth_rejected_credential(self, mock_urlopen):
        """Verify rejected GoTrue credentials fall back to local Django DB authentication."""
        import urllib.error

        from extractor.auth import SupabaseAuthBackend

        # Mock HTTPError 400 Bad Request
        mock_err = urllib.error.HTTPError(
            url="https://project.supabase.co/auth/v1/token", code=400, msg="Bad Request", hdrs={}, fp=MagicMock()
        )
        mock_err.read = MagicMock(return_value=b'{"error": "invalid_credentials"}')
        mock_urlopen.side_effect = mock_err

        # Check that it falls back to authenticating against the local Django DB
        backend = SupabaseAuthBackend()
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            # Authentic user from local Django DB (created in setUp) should still login successfully
            user_by_uname = backend.authenticate(None, username=self.username, password=self.password)
            # Since username doesn't have '@', SupabaseAuthBackend falls back to local DB directly without GoTrue dispatch!
            self.assertIsNotNone(user_by_uname)
            self.assertEqual(user_by_uname.username, self.username)

    @patch("urllib.request.urlopen")
    def test_supabase_email_prefix_collision(self, mock_urlopen):
        """Verify that when registering two users with the same email prefix but different domains, no IntegrityError occurs."""
        import json

        from extractor.auth import SupabaseAuthBackend

        # User 1 is already in DB (username='scholar.test', email='scholar.test@example.com')
        # Let's try to authenticate 'scholar.test@different-domain.com'
        mock_response_data = {
            "user": {"id": "supabase-uuid-999", "email": "scholar.test@different-domain.com"},
            "access_token": "mock-jwt-token-999",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            user2 = backend.authenticate(None, username="scholar.test@different-domain.com", password="somepassword")

        self.assertIsNotNone(user2)
        self.assertEqual(user2.email, "scholar.test@different-domain.com")
        # Username should have the unique hash appended
        self.assertNotEqual(user2.username, "scholar.test")
        self.assertTrue(user2.username.startswith("scholar.test_"))

    def test_standard_user_isolation_on_document_views(self):
        """Verify that standard (non-staff) users are denied access to other users' documents or system documents they didn't upload."""
        # Create another user and their document
        other_user = User.objects.create_user(username="otheruser", password="password123")
        other_doc = SourceDocument.objects.create(
            original_filename="other.pdf",
            file_hash="other-hash",
            title="Other User's Doc",
            status="COMPLETED",
            uploaded_by=other_user,
        )

        # Document uploaded by self
        my_doc = SourceDocument.objects.create(
            original_filename="mine.pdf",
            file_hash="my-hash",
            title="My Doc",
            status="COMPLETED",
            uploaded_by=self.user,
        )

        # Log in as self.user (standard user)
        self.client.login(username=self.username, password=self.password)

        # 1. Detail View Checks
        # - Accessing system-wide doc (uploaded_by=None) -> Allowed (200)
        response = self.client.get(reverse("document_detail", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 200)

        # - Accessing own doc -> Allowed (200)
        response = self.client.get(reverse("document_detail", args=[my_doc.uuid]))
        self.assertEqual(response.status_code, 200)

        # - Accessing other user's doc -> Redirected / Forbidden (302 redirect to dashboard)
        response = self.client.get(reverse("document_detail", args=[other_doc.uuid]))
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.url.endswith(reverse("dashboard")))

        # 2. Save View Checks
        # - Modifying system-wide doc -> Redirected / Forbidden (302)
        response = self.client.post(reverse("save_document", args=[self.doc.uuid]), {"refined_markdown": "edited"})
        self.assertEqual(response.status_code, 302)

        # - Modifying own doc -> Allowed (302 to detail page)
        with patch("extractor.views.transaction.on_commit", lambda f: f()):
            with patch("extractor.cloud_tasks.enqueue"):
                response = self.client.post(
                    reverse("save_document", args=[my_doc.uuid]), {"refined_markdown": "edited"}
                )
                self.assertEqual(response.status_code, 302)
                my_doc.refresh_from_db()
                self.assertEqual(my_doc.refined_markdown, "edited")

        # - Modifying other user's doc -> Redirected / Forbidden (302 to dashboard)
        response = self.client.post(reverse("save_document", args=[other_doc.uuid]), {"refined_markdown": "edited"})
        self.assertEqual(response.status_code, 302)

        # 3. Retry View Checks
        # - Retrying system-wide doc -> Redirected / Forbidden (302)
        response = self.client.post(reverse("retry_document", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 302)

        # - Retrying own doc -> Allowed (302 redirect to dashboard or JSON)
        with patch("extractor.cloud_tasks.enqueue"):
            response = self.client.post(reverse("retry_document", args=[my_doc.uuid]))
            self.assertEqual(response.status_code, 302)

        # - Retrying other user's doc -> Redirected / Forbidden (302)
        response = self.client.post(reverse("retry_document", args=[other_doc.uuid]))
        self.assertEqual(response.status_code, 302)

        # 4. Delete View Checks
        # - Deleting system-wide doc -> Redirected / Forbidden (302)
        response = self.client.post(reverse("delete_document", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 302)

        # - Deleting other user's doc -> Redirected / Forbidden (302)
        response = self.client.post(reverse("delete_document", args=[other_doc.uuid]))
        self.assertEqual(response.status_code, 302)

        # - Deleting own doc -> Allowed (302 redirect)
        response = self.client.post(reverse("delete_document", args=[my_doc.uuid]))
        self.assertEqual(response.status_code, 302)


class DynamicCsrfMiddlewareTestCase(TestCase):
    """Verifies that DynamicCsrfTrustedOriginsMiddleware correctly whitelists origins in DEBUG mode."""

    def setUp(self):
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False

    def tearDown(self):
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False

    def test_dynamic_csrf_middleware_adds_origin(self):

        # Backup the current CSRF_TRUSTED_ORIGINS
        original_origins = list(settings.CSRF_TRUSTED_ORIGINS)

        try:
            factory = RequestFactory()
            # Simulate a request from a custom domain
            request = factory.post(
                "/",
                HTTP_HOST="custom-tunnel-domain.example.com",
                HTTP_ORIGIN="https://custom-tunnel-domain.example.com",
            )

            # Ensure DEBUG is True
            with self.settings(DEBUG=True):
                # Call middleware
                from core.middleware import DynamicCsrfTrustedOriginsMiddleware

                middleware = DynamicCsrfTrustedOriginsMiddleware(lambda req: None)
                middleware(request)

                # Check if the origin and host have been added to CSRF_TRUSTED_ORIGINS
                self.assertIn("https://custom-tunnel-domain.example.com", settings.CSRF_TRUSTED_ORIGINS)
                self.assertIn("http://custom-tunnel-domain.example.com", settings.CSRF_TRUSTED_ORIGINS)

        finally:
            # Restore original settings
            settings.CSRF_TRUSTED_ORIGINS = original_origins

    def test_dynamic_csrf_middleware_database_origins(self):
        # Backup the current CSRF_TRUSTED_ORIGINS
        original_origins = list(settings.CSRF_TRUSTED_ORIGINS)

        try:
            # Configure database trusted origins
            settings_obj = SystemSettings.get_settings()
            settings_obj.csrf_trusted_origins = "https://db-configured-domain.com, http://another-db-domain.com"
            settings_obj.save()

            factory = RequestFactory()
            request = factory.post("/")

            # Call middleware in production mode (DEBUG=False)
            with self.settings(DEBUG=False):
                from core.middleware import DynamicCsrfTrustedOriginsMiddleware

                middleware = DynamicCsrfTrustedOriginsMiddleware(lambda req: None)
                middleware(request)

                # Check that the database-configured origins are trusted even when DEBUG=False
                self.assertIn("https://db-configured-domain.com", settings.CSRF_TRUSTED_ORIGINS)
                self.assertIn("http://another-db-domain.com", settings.CSRF_TRUSTED_ORIGINS)

        finally:
            # Restore original settings and clear database setting
            settings.CSRF_TRUSTED_ORIGINS = original_origins
            settings_obj = SystemSettings.get_settings()
            settings_obj.csrf_trusted_origins = ""
            settings_obj.save()

    def test_dynamic_csrf_middleware_adds_loopback_in_production(self):
        # Backup the current CSRF_TRUSTED_ORIGINS
        original_origins = list(settings.CSRF_TRUSTED_ORIGINS)

        try:
            factory = RequestFactory()
            # Simulate a request from localhost but with DEBUG=False
            request = factory.post(
                "/",
                HTTP_HOST="localhost:8080",
                HTTP_ORIGIN="http://localhost:8080",
                HTTP_REFERER="http://localhost:8080/login/",
            )

            with self.settings(DEBUG=False):
                from core.middleware import DynamicCsrfTrustedOriginsMiddleware

                middleware = DynamicCsrfTrustedOriginsMiddleware(lambda req: None)
                middleware(request)

                # Check if the origin has been added to CSRF_TRUSTED_ORIGINS even in production
                self.assertIn("http://localhost:8080", settings.CSRF_TRUSTED_ORIGINS)

        finally:
            settings.CSRF_TRUSTED_ORIGINS = original_origins

    def test_dynamic_csrf_middleware_db_cooldown(self):
        original_origins = list(settings.CSRF_TRUSTED_ORIGINS)
        try:
            from core.middleware import DynamicCsrfTrustedOriginsMiddleware

            # Force cache reset
            DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False
            DynamicCsrfTrustedOriginsMiddleware._last_query_time = 0.0

            settings_obj = SystemSettings.get_settings()
            settings_obj.csrf_trusted_origins = "https://initial-origin.com"
            settings_obj.save()

            factory = RequestFactory()
            request = factory.post("/")

            middleware = DynamicCsrfTrustedOriginsMiddleware(lambda req: None)

            # First execution loads initial settings
            middleware(request)
            self.assertIn("https://initial-origin.com", settings.CSRF_TRUSTED_ORIGINS)

            # Update settings in DB
            settings_obj.csrf_trusted_origins = "https://initial-origin.com, https://updated-origin.com"
            settings_obj.save()

            # Second execution immediately after should NOT load because of 60s cooldown
            middleware(request)
            self.assertNotIn("https://updated-origin.com", settings.CSRF_TRUSTED_ORIGINS)

            # Manually simulate time passing beyond cooldown
            import time

            DynamicCsrfTrustedOriginsMiddleware._last_query_time = time.time() - 61.0

            # Third execution should now reload and pick up the new origin
            middleware(request)
            self.assertIn("https://updated-origin.com", settings.CSRF_TRUSTED_ORIGINS)

        finally:
            settings.CSRF_TRUSTED_ORIGINS = original_origins
            settings_obj = SystemSettings.get_settings()
            settings_obj.csrf_trusted_origins = ""
            settings_obj.save()


class AuditLogTestCase(TestCase):
    """Verifies that system audit logs are cleanly queried, filtered, and rendered safely."""

    def setUp(self):
        # Create users
        self.normal_user = User.objects.create_user(username="normaluser", password="password123")
        self.staff_user = User.objects.create_user(username="staffuser", password="password123", is_staff=True)

        # Create documents
        self.doc1 = SourceDocument.objects.create(
            original_filename="doc1.pdf",
            file_hash="hash1",
            title="Document 1",
            status="COMPLETED",
        )
        self.doc2 = SourceDocument.objects.create(
            original_filename="doc2.pdf",
            file_hash="hash2",
            title="Document 2",
            status="COMPLETED",
        )

        # Create audit logs
        self.log_normal = AuditLog.objects.create(
            user=self.normal_user,
            action=AuditAction.UPLOAD,
            document=self.doc1,
            details="Normal user uploaded doc1",
            ip_address="127.0.0.1",
        )
        self.log_staff = AuditLog.objects.create(
            user=self.staff_user,
            action=AuditAction.DELETE,
            document=self.doc2,
            details="Staff user deleted doc2",
            ip_address="10.0.0.1",
        )

    def test_audit_logs_list_normal_user_isolation(self):
        self.client.login(username="normaluser", password="password123")
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        logs = response.context["logs"]
        # Normal users should only see their own logs (the UPLOAD log + the LOGIN log)
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log.user, self.normal_user)

    def test_audit_logs_list_staff_user_all(self):
        self.client.login(username="staffuser", password="password123")
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        logs = response.context["logs"]
        # Staff/Superusers should see all logs (the normal user upload, staff delete, plus the login logs)
        self.assertEqual(len(logs), 3)

    def test_audit_logs_filtering(self):
        self.client.login(username="staffuser", password="password123")

        # Filter by action
        response = self.client.get(reverse("audit_logs") + "?action=" + AuditAction.DELETE)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["logs"]), 1)
        self.assertEqual(response.context["logs"][0].action, AuditAction.DELETE)

        # Filter by user
        response = self.client.get(reverse("audit_logs") + "?user=normaluser")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["logs"]), 1)
        self.assertEqual(response.context["logs"][0].user.username, "normaluser")

        # Filter by search query
        response = self.client.get(reverse("audit_logs") + "?q=doc1")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.context["logs"]), 1)
        self.assertIn("doc1", response.context["logs"][0].details)

    def test_audit_logs_dangling_document(self):
        # Create an audit log referencing a document, then delete the document
        log_dangling = AuditLog.objects.create(
            user=self.normal_user,
            action=AuditAction.EXTRACTION_COMPLETED,
            document=self.doc1,
            details="Completed extraction for doc1",
            ip_address="127.0.0.1",
        )
        self.doc1.delete()
        # Since on_delete=models.SET_NULL, log_dangling.document should now be None
        log_dangling.refresh_from_db()
        self.assertIsNone(log_dangling.document)

        # Login and verify the view still renders successfully without 500 error
        self.client.login(username="normaluser", password="password123")
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"--", response.content)


class DeploymentControllerViewTestCase(TestCase):
    """Verifies DeploymentControllerView behaviour under various roles and scaling options."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username="depadmin", password="password123")
        self.staff = User.objects.create_user(username="depstaff", password="password123", is_staff=True)
        self.user = User.objects.create_user(username="depuser", password="password123")
        self.client.login(username="depadmin", password="password123")

        # Class-wide mocks for deployment helpers
        self.patchers = [
            patch("extractor.views.DeploymentControllerView.test_func", return_value=True),
            patch("extractor.deployment.get_gcp_project_details"),
            patch("extractor.deployment.get_service_config"),
            patch("extractor.deployment.get_service_logs"),
            patch("extractor.deployment.run_qa_checks"),
            patch("extractor.deployment.update_service_scale"),
        ]
        self.mocks = [p.start() for p in self.patchers]

        # Set default values for mocks
        self.mocks[1].return_value = {"project_id": "test-project-123", "region": "us-central1"}
        self.mocks[2].return_value = {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "autoscaling.knative.dev/minScale": "0",
                            "autoscaling.knative.dev/maxScale": "0",
                        }
                    }
                }
            }
        }
        self.mocks[3].return_value = []
        self.mocks[4].return_value = "QA OK"
        self.mocks[5].return_value = {"status": "success"}

    def tearDown(self):
        for p in self.patchers:
            p.stop()

    def test_anonymous_redirect(self):
        self.client.logout()
        # Temporarily stop the test_func override to test redirection
        self.patchers[0].stop()
        try:
            response = self.client.get(reverse("deployment_controller"))
            self.assertEqual(response.status_code, 302)
        finally:
            self.patchers[0].start()

    def test_non_staff_forbidden(self):
        self.client.login(username="depuser", password="password123")
        # Temporarily stop the test_func override to test forbidden status
        self.patchers[0].stop()
        try:
            response = self.client.get(reverse("deployment_controller"))
            self.assertEqual(response.status_code, 403)
        finally:
            self.patchers[0].start()

    def test_get_dashboard_gcp_active(self):
        # Configure mocks specifically for this test
        self.mocks[2].side_effect = lambda service: {
            "spec": {
                "template": {
                    "metadata": {
                        "annotations": {
                            "autoscaling.knative.dev/minScale": "1" if service == "data-extractor-web" else "0",
                            "autoscaling.knative.dev/maxScale": "5" if service == "data-extractor-web" else "3",
                        }
                    }
                }
            }
        }
        self.mocks[3].return_value = [{"timestamp": "2026-06-20", "message": "Log msg", "severity": "INFO"}]

        response = self.client.get(reverse("deployment_controller"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test-project-123")
        self.assertContains(response, "us-central1")
        self.assertEqual(response.context["worker_min"], 0)
        self.assertEqual(response.context["worker_max"], 3)
        self.assertEqual(response.context["web_min"], 1)
        self.assertEqual(response.context["web_max"], 5)
        self.assertEqual(response.context["current_mode"], "on-demand")

    def test_get_dashboard_gcp_inactive_fallback(self):
        self.mocks[1].return_value = {"project_id": "fallback-project", "region": "asia-southeast1"}
        self.mocks[2].side_effect = Exception("GCP connection error")

        with self.assertLogs("extractor.views", level="WARNING") as log_capture:
            response = self.client.get(reverse("deployment_controller"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["worker_min"], 0)
        self.assertEqual(response.context["worker_max"], 0)
        self.assertEqual(response.context["web_min"], 1)
        self.assertEqual(response.context["web_max"], 5)
        self.assertEqual(response.context["current_mode"], "hibernate")
        self.assertTrue(any("Could not load worker config from GCP" in message for message in log_capture.output))
        self.assertTrue(any("Could not load web config from GCP" in message for message in log_capture.output))

    def test_post_hibernate(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "hibernate"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("data-extractor-worker", 0, 1)

    def test_post_on_demand(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "on-demand"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("data-extractor-worker", 0, 5)

    def test_post_always_on(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "always-on"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("data-extractor-worker", 1, 5)

    def test_post_invalid(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "invalid-mode"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_not_called()


class UserIsolationDashboardAndRAGTestCase(TestCase):
    """Verifies that dashboard stats, document lists, and RAG searches enforce user isolation."""

    def setUp(self):
        from django.contrib.auth.models import User

        self.user_a = User.objects.create_user(username="user_a", password="password123")
        self.user_b = User.objects.create_user(username="user_b", password="password123")

        # User A's document
        self.doc_a = SourceDocument.objects.create(
            original_filename="doc_a.txt",
            file_hash="hash-a",
            title="User A's Sacred Document",
            status="COMPLETED",
            uploaded_by=self.user_a,
            cost_usd=Decimal("0.02"),
            input_tokens=100,
            output_tokens=50,
            page_count=1,
        )

        # User B's document
        self.doc_b = SourceDocument.objects.create(
            original_filename="doc_b.txt",
            file_hash="hash-b",
            title="User B's Private Document",
            status="COMPLETED",
            uploaded_by=self.user_b,
            cost_usd=Decimal("0.05"),
            input_tokens=200,
            output_tokens=100,
            page_count=2,
        )

        # System/Shared document (uploaded_by=None)
        self.doc_system = SourceDocument.objects.create(
            original_filename="doc_system.txt",
            file_hash="hash-system",
            title="System Wide Shared Document",
            status="COMPLETED",
            uploaded_by=None,
            cost_usd=Decimal("0.01"),
            input_tokens=50,
            output_tokens=20,
            page_count=1,
        )

        # No pgvector chunks to create (now stored in SurrealDB)
        pass

    def test_dashboard_stats_and_document_list_are_user_isolated(self):
        self.client.login(username="user_a", password="password123")

        # 1. Check Dashboard View
        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

        # Dashboard docs should contain doc_a and doc_system, but NOT doc_b
        docs_in_context = [item["obj"] for item in response.context["documents"]]
        self.assertIn(self.doc_a, docs_in_context)
        self.assertIn(self.doc_system, docs_in_context)
        self.assertNotIn(self.doc_b, docs_in_context)

        # Total spent statistics for user_a should be doc_a + doc_system = 0.02 + 0.01 = 0.03
        self.assertEqual(response.context["total_spent_usd"], Decimal("0.03"))

        # 2. Check DocumentStatusAPIView
        response = self.client.get(reverse("document_status_api"))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        doc_ids = [d["id"] for d in data["documents"]]
        self.assertIn(self.doc_a.id, doc_ids)
        self.assertIn(self.doc_system.id, doc_ids)
        self.assertNotIn(self.doc_b.id, doc_ids)
        self.assertEqual(data["stats"]["monthly_spent"], 0.03)

    @patch("extractor.rag.generate_llm_content_unified")
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback")
    @patch("extractor.surreal_db.search_chunks_hnsw")
    def test_rag_search_user_isolated(self, mock_search_chunks, mock_execute, mock_generate):
        self.client.login(username="user_a", password="password123")

        # Mock the embedding call response for search query
        mock_emb_val = MagicMock()
        mock_emb_val.values = [1.0, 0.0] * 384
        mock_query_resp = MagicMock()
        mock_query_resp.embeddings = [mock_emb_val]
        mock_execute.return_value = mock_query_resp

        # Mock SurrealDB chunk search
        mock_search_chunks.return_value = [
            {"doc_uuid": str(self.doc_a.uuid), "content": "sacred-content-a: User A text", "chunk_index": 0}
        ]

        # Mock RAG answer generator
        mock_unified_resp = MagicMock()
        mock_unified_resp.text = "Answer generated using user A context only."
        mock_generate.return_value = mock_unified_resp

        with self.settings(GEMINI_API_KEY="mock-api-key"):
            response = self.client.get(reverse("rag_search"), {"q": "sacred-content-a"})
            self.assertEqual(response.status_code, 200)
            data = response.json()

            # Ensure answer is correct and sources only contain User A's doc / system doc, but NOT User B's doc
            self.assertEqual(data["answer"], "Answer generated using user A context only.")
            source_ids = [s["id"] for s in data["sources"]]
            self.assertIn(self.doc_a.id, source_ids)
            self.assertNotIn(self.doc_b.id, source_ids)


class SecurityAuthTestCase(TestCase):
    """Verifies that various authentication, registration, recovery, and settings input checks are secure."""



    def test_register_view_rejects_reserved_emails(self):
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            # Register user with admin@
            response = self.client.post(
                reverse("register"),
                {"email": "admin@something.com", "password": "password123", "confirm_password": "password123"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Registration of administrative or system email addresses is not permitted.")

            # Register user with supabase domain
            response = self.client.post(
                reverse("register"),
                {
                    "email": "attacker@project.supabase.co",
                    "password": "password123",
                    "confirm_password": "password123",
                },
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Registration of administrative or system email addresses is not permitted.")

    def test_register_view_validates_email_format(self):
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            response = self.client.post(
                reverse("register"),
                {"email": "invalid-email-format", "password": "password123", "confirm_password": "password123"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Invalid email format.")

    @patch("urllib.request.urlopen")
    def test_register_view_scheme_validation(self, mock_urlopen):
        # Attempt to register when SUPABASE_URL has an insecure HTTP scheme in production (DEBUG=False)
        with self.settings(
            SUPABASE_URL="http://insecure-supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key", DEBUG=False
        ):
            response = self.client.post(
                reverse("register"),
                {"email": "test@example.com", "password": "password123", "confirm_password": "password123"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Insecure URL scheme. Production environments require https.")

    @patch("urllib.request.urlopen")
    def test_forgot_password_scheme_validation(self, mock_urlopen):
        # Attempt recovery with insecure HTTP URL when DEBUG=False
        with self.settings(
            SUPABASE_URL="http://insecure-supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key", DEBUG=False
        ):
            response = self.client.post(reverse("forgot_password"), {"email": "test@example.com"})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "Insecure URL scheme. Production environments require https.")

    @patch("urllib.request.urlopen")
    def test_supabase_admin_promotion_security(self, mock_urlopen):
        # Mock successful login response from Supabase for a normal user email but trying to log in as "admin"
        import json

        from extractor.auth import SupabaseAuthBackend

        # User email is not admin@<domain>
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps(
            {"user": {"id": "mock-uuid-1", "email": "normal_user@example.com"}}
        ).encode("utf-8")
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(
            SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key", DEBUG=False
        ):
            # Pass "admin" as username but Supabase returns "normal_user@example.com"
            user = backend.authenticate(None, username="admin", password="password123")

            self.assertIsNotNone(user)
            # Normal user should NOT be promoted to superuser/staff
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.is_staff)


class BulkDocumentActionTestCase(TestCase):
    """Verifies bulk reprocess/restart and bulk deletion actions."""

    def setUp(self):
        self.password = "T00rP@ssw0rd!"
        self.user_email = "scholar.test@example.com"
        self.username = "scholar.test"
        self.user = User.objects.create_user(username=self.username, email=self.user_email, password=self.password)

        self.doc1 = SourceDocument.objects.create(
            original_filename="doc1.pdf",
            file_hash="hash1",
            title="Doc 1",
            status="COMPLETED",
            uploaded_by=self.user,
        )
        self.doc2 = SourceDocument.objects.create(
            original_filename="doc2.pdf",
            file_hash="hash2",
            title="Doc 2",
            status="FAILED",
            uploaded_by=self.user,
        )

    @patch("extractor.cloud_tasks.enqueue")
    def test_bulk_restart(self, mock_enqueue):
        self.client.login(username=self.username, password=self.password)

        response = self.client.post(
            reverse("bulk_action"),
            {
                "action": "restart",
                "selected_documents": [self.doc1.id, self.doc2.id],
            },
        )
        self.assertEqual(response.status_code, 302)

        self.doc1.refresh_from_db()
        self.doc2.refresh_from_db()

        # Verify status reset to PENDING
        self.assertEqual(self.doc1.status, "PENDING")
        self.assertEqual(self.doc2.status, "PENDING")
        self.assertEqual(self.doc1.retry_count, 0)
        self.assertEqual(self.doc2.retry_count, 0)

    @patch("django.core.files.storage.default_storage.exists", return_value=False)
    @patch("django.core.files.storage.default_storage.delete")
    @patch("extractor.surreal_db.delete_chunks")
    def test_bulk_delete(self, mock_delete_chunks, mock_storage_delete, mock_storage_exists):
        self.client.login(username=self.username, password=self.password)

        response = self.client.post(
            reverse("bulk_action"),
            {
                "action": "delete",
                "selected_documents": [self.doc1.id, self.doc2.id],
            },
        )
        self.assertEqual(response.status_code, 302)

        # Verify they are deleted from DB
        self.assertEqual(SourceDocument.objects.filter(id__in=[self.doc1.id, self.doc2.id]).count(), 0)


class CoreDesignHardeningTests(TestCase):
    def setUp(self):
        self.username = "test_user_hardening"
        self.email = "test_hardening@example.com"
        self.password = "Secr3tPass!"
        self.user = User.objects.create_user(
            username=self.username, email=self.email, password=self.password
        )
        self.client.login(username=self.username, password=self.password)

    def test_openrouter_api_key_masked_property(self):
        settings_obj = SystemSettings.get_settings()
        settings_obj.openrouter_api_key = ""
        self.assertEqual(settings_obj.openrouter_api_key_masked, "")

        settings_obj.openrouter_api_key = "sk-or-v1-supersecretkey"
        settings_obj.save()
        self.assertEqual(settings_obj.openrouter_api_key_masked, "••••••••••••••••")

    def test_save_settings_view_post_api_key_masked(self):
        settings_obj = SystemSettings.get_settings()
        settings_obj.openrouter_api_key = "sk-or-v1-originalkey"
        settings_obj.save()

        # Admin user required to edit settings
        self.user.is_staff = True
        self.user.save()

        # Post with the masked value should not overwrite original key
        response = self.client.post(
            reverse("save_settings"),
            {
                "monthly_budget_usd": "100.00",
                "selected_model": "auto",
                "openrouter_api_key": "••••••••••••••••",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.openrouter_api_key, "sk-or-v1-originalkey")

        # Post with empty value should clear the key
        response = self.client.post(
            reverse("save_settings"),
            {
                "monthly_budget_usd": "100.00",
                "selected_model": "auto",
                "openrouter_api_key": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.openrouter_api_key, "")

        # Post with new value should update the key
        response = self.client.post(
            reverse("save_settings"),
            {
                "monthly_budget_usd": "100.00",
                "selected_model": "auto",
                "openrouter_api_key": "sk-or-v1-newkey",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_obj.refresh_from_db()
        self.assertEqual(settings_obj.openrouter_api_key, "sk-or-v1-newkey")

    @patch("extractor.views.query_semantic_knowledge_rag")
    def test_document_rag_search_view_with_document_ids(self, mock_rag):
        mock_rag.return_value = {"answer": "this is filtered answer", "sources": []}
        response = self.client.get(reverse("rag_search") + "?q=filtered-query&document_ids=5,12")
        self.assertEqual(response.status_code, 200)
        mock_rag.assert_called_once_with("filtered-query", document_ids=[5, 12], top_k=5, user=self.user)
