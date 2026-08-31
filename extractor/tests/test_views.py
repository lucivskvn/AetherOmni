from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.test import RequestFactory, TestCase
from django.urls import reverse

from extractor.models import AuditAction, AuditLog, SourceDocument, SystemSettings
from extractor.utils import AuditEvent, log_audit_event
from extractor.views import _parse_surreal_audit_details, _parse_surreal_audit_log, get_request_actor_id


class ViewsTestCase(TestCase):
    """Verifies all Dashboard, Settings, Upload, Detail, Save, Delete, Purge, and Search Views."""

    def setUp(self):
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False

        self.user = User.objects.create_superuser(
            username="testviewuser", email="testviewuser@example.com", password="password123"
        )
        self.client.force_login(self.user)

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

    def test_dashboard_view_multiple_status_filtering(self):
        # Create a pending document in addition to completed self.doc
        pending_doc = SourceDocument.objects.create(
            original_filename="pending_doc.pdf",
            status="PENDING",
            uploaded_by=self.user,
        )
        response = self.client.get(reverse("dashboard") + "?status=COMPLETED&status=PENDING")
        self.assertEqual(response.status_code, 200)
        docs = [d["obj"] for d in response.context["documents"]]
        self.assertIn(self.doc, docs)
        self.assertIn(pending_doc, docs)

    def test_upload_validation_rejects_filename_without_a_title(self):
        from extractor.views import _validate_upload_file

        result = _validate_upload_file("---.pdf", 1)

        self.assertEqual(result["status"], "error")
        self.assertIn("meaningful title", result["error"])

    @patch.dict("os.environ", {"RELEASE_VERSION": "1.2.3", "BUILD_SHA": "1234567890abcdef"})
    def test_release_metadata_view_reports_runtime_environment(self):
        response = self.client.get(reverse("release_metadata"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"release_version": "1.2.3", "commit_sha": "1234567"})
        self.assertEqual(response["Cache-Control"], "no-store")

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
                "csrf_trusted_origins": "https://my-good-domain.com, https://my-second-domain.com",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_refreshed = SystemSettings.get_settings()
        self.assertEqual(settings_refreshed.monthly_budget_usd, Decimal("25.50"))
        self.assertEqual(
            settings_refreshed.csrf_trusted_origins, "https://my-good-domain.com, https://my-second-domain.com"
        )

    def test_save_settings_view_post_invalid_budget(self):
        response = self.client.post(reverse("save_settings"), {"monthly_budget_usd": "-10.00"})
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
        with (
            patch("extractor.views.transaction.on_commit", lambda f: f()),
            patch("extractor.cloud_tasks.enqueue") as mock_enqueue,
        ):
            response = self.client.post(
                reverse("save_document", args=[self.doc.uuid]),
                {
                    "refined_markdown": "### Highly Refined Markdown",
                    "publisher": "Cambridge University Press",
                    "publication_year": "2025",
                    "license_type": "MIT",
                    "doi": "10.1017/cup.2025",
                },
            )
            self.assertEqual(response.status_code, 302)
            self.doc.refresh_from_db()
            self.assertEqual(self.doc.refined_markdown, "### Highly Refined Markdown")
            self.assertEqual(self.doc.publisher, "Cambridge University Press")
            self.assertEqual(self.doc.publication_year, "2025")
            self.assertEqual(self.doc.license_type, "MIT")
            self.assertEqual(self.doc.doi, "10.1017/cup.2025")
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
        from django.core.cache import cache

        cache.clear()
        mock_zip.return_value = b"fake-zip-data"
        response = self.client.post(reverse("export_zip"), {"selected_documents": [self.doc.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/zip")
        self.assertEqual(response.content, b"fake-zip-data")

    def test_export_sft_jsonl_empty(self):
        response = self.client.post(reverse("export_sft_jsonl"))
        self.assertEqual(response.status_code, 302)

    @patch("extractor.views.generate_sft_jsonl_bundle")
    def test_export_sft_jsonl_success(self, mock_jsonl):
        from django.core.cache import cache

        cache.clear()
        mock_jsonl.return_value = b'{"prompt": "Q", "completion": "A"}\n'
        response = self.client.post(reverse("export_sft_jsonl"), {"selected_documents": [str(self.doc.uuid)]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-jsonlines")
        self.assertIn(b'{"prompt": "Q"', response.content)

    @patch("extractor.views.generate_sft_dataset_pairs")
    def test_sft_dataset_preview_success(self, mock_pairs):
        mock_pairs.return_value = [
            {"prompt": "Test Question", "completion": "Test Answer", "metadata": {"page_number": 1}}
        ]
        response = self.client.get(reverse("sft_dataset_preview", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["status"], "success")
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["pairs"][0]["prompt"], "Test Question")

    @patch("extractor.views.generate_sft_dataset_pairs")
    def test_sft_dataset_preview_error(self, mock_pairs):
        mock_pairs.side_effect = ValueError("Document chunk not found")
        response = self.client.get(reverse("sft_dataset_preview", args=[self.doc.uuid]))
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data["status"], "error")

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
        self.client.force_login(self.user)

        response = self.client.get(reverse("dashboard"))
        self.assertEqual(response.status_code, 200)

    def test_force_password_change_for_admin_password(self):
        """Verify that a user logged in with the default password 'admin' is forced to change it."""
        # Create a user with password 'admin'
        _default_user = User.objects.create_user(username="defaultadmin", password="admin")
        self.client.force_login(_default_user)

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
        request = MagicMock()
        request.POST = {"cf-turnstile-response": "valid-captcha-token"}
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            user = backend.authenticate(request, username="supabase.test@example.com", password="somepassword")

        self.assertIsNotNone(user)
        self.assertEqual(user.email, "supabase.test@example.com")
        self.assertEqual(user.username, "supabase.test")
        request_body = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("captcha_token", request_body)
        self.assertEqual(request_body["gotrue_meta_security"], {"captcha_token": "valid-captcha-token"})

    @patch("urllib.request.urlopen")
    def test_supabase_idp_auth_rejected_credential(self, mock_urlopen):
        """Verify rejected GoTrue credentials never fall back to local Django authentication."""
        import urllib.error

        from extractor.auth import SupabaseAuthBackend

        # Mock HTTPError 400 Bad Request
        mock_err = urllib.error.HTTPError(
            url="https://project.supabase.co/auth/v1/token", code=400, msg="Bad Request", hdrs={}, fp=MagicMock()
        )
        mock_err.read = MagicMock(return_value=b'{"error": "invalid_credentials"}')
        mock_urlopen.side_effect = mock_err

        backend = SupabaseAuthBackend()
        with self.settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="mock-public-key"):
            authenticated = backend.authenticate(None, username=self.user_email, password=self.password)

        self.assertIsNone(authenticated)

    @patch("urllib.request.urlopen")
    def test_turnstile_protected_supabase_rejection_does_not_fallback_locally(self, mock_urlopen):
        """A rejected protected GoTrue request must not bypass CAPTCHA via local auth."""
        import urllib.error

        from extractor.auth import SupabaseAuthBackend

        mock_err = urllib.error.HTTPError(
            url="https://project.supabase.co/auth/v1/token", code=400, msg="Bad Request", hdrs={}, fp=MagicMock()
        )
        mock_err.read = MagicMock(return_value=b'{"error": "captcha_failed"}')
        mock_urlopen.side_effect = mock_err
        request = MagicMock()
        request.POST = {"cf-turnstile-response": "invalid-captcha-token"}

        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="test-site-key",
        ):
            user = SupabaseAuthBackend().authenticate(request, username=self.user_email, password=self.password)

        self.assertIsNone(user)

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
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="",
        ):
            user2 = backend.authenticate(None, username="scholar.test@different-domain.com", password="somepassword")

        self.assertIsNotNone(user2)
        self.assertEqual(user2.email, "scholar.test@different-domain.com")
        # Username should have the unique hash appended
        self.assertNotEqual(user2.username, "scholar.test")
        self.assertTrue(user2.username.startswith("scholar.test_"))

    @patch("urllib.request.urlopen")
    def test_supabase_email_prefix_collision_multiple(self, mock_urlopen):
        """Verify that multiple colliding email prefixes/usernames are handled sequentially via the loop."""
        import hashlib
        import json

        from django.contrib.auth.models import User

        from extractor.auth import SupabaseAuthBackend

        # pre-create user with conflicting hash suffix
        email_hash = hashlib.sha256(b"scholar.test@different-domain.com").hexdigest()[:8]
        User.objects.create_user(username=f"scholar.test_{email_hash}", email="st_hash@other.com")

        mock_response_data = {
            "user": {"id": "supabase-uuid-collision-multi", "email": "scholar.test@different-domain.com"},
            "access_token": "mock-jwt-token-collision-multi",
        }
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(mock_response_data).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="",
        ):
            user = backend.authenticate(None, username="scholar.test@different-domain.com", password="somepassword")

        self.assertIsNotNone(user)
        self.assertEqual(user.email, "scholar.test@different-domain.com")
        # Since scholar.test_{hash} was taken by st_hash@other.com, it should loop to attempt=1 suffix: scholar.test_1
        self.assertEqual(user.username, "scholar.test_1")

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
        self.client.force_login(self.user)

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
        with (
            patch("extractor.views.transaction.on_commit", lambda f: f()),
            patch("extractor.cloud_tasks.enqueue"),
        ):
            response = self.client.post(reverse("save_document", args=[my_doc.uuid]), {"refined_markdown": "edited"})
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

    def test_document_cancel_view(self):
        self.client.force_login(self.user)
        doc_in_flight = SourceDocument.objects.create(
            original_filename="in_flight.pdf",
            file_hash="mock-hash-inflight",
            title="In Flight Doc",
            status="EXTRACTING",
            uploaded_by=self.user,
        )

        # Test AJAX cancellation
        response = self.client.post(
            reverse("cancel_document", args=[doc_in_flight.uuid]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "success")

        doc_in_flight.refresh_from_db()
        self.assertEqual(doc_in_flight.status, "FAILED")
        self.assertIn("stopped by user", doc_in_flight.error_message)

    def test_document_cancel_view_permission_denied(self):
        other_user = User.objects.create_user(
            username="another_user", email="another_user@example.com", password="password123"
        )
        other_doc = SourceDocument.objects.create(
            original_filename="other_inflight.pdf",
            file_hash="mock-hash-other-inflight",
            title="Other In Flight Doc",
            status="PENDING",
            uploaded_by=other_user,
        )

        std_user = User.objects.create_user(
            username="regular_user", email="regular_user@example.com", password="password123"
        )
        self.client.force_login(std_user)

        response = self.client.post(
            reverse("cancel_document", args=[other_doc.uuid]),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
            HTTP_ACCEPT="application/json",
        )
        self.assertEqual(response.status_code, 403)

    def test_clone_deduplicated_doc_legal_metadata_preservation(self):
        """Verify _clone_deduplicated_doc preserves publisher, publication_year, license_type, doi."""
        from extractor.views import _clone_deduplicated_doc

        user = User.objects.create_user(username="dedup_tester", password="Password123!")
        existing_doc = SourceDocument.objects.create(
            original_filename="legal_paper.pdf",
            file_hash="unique-legal-hash-12345",
            title="Scholarly Legal Corpus",
            author="Dr. Legal Scholar",
            publisher="Oxford Academic Press",
            publication_year="2026",
            license_type="CC-BY-4.0",
            doi="10.1093/oxford/2026.01",
            status="COMPLETED",
            uploaded_by=user,
        )

        request = MagicMock()
        request.user = user
        request.META = {"REMOTE_ADDR": "127.0.0.1"}

        with (
            patch("extractor.surreal_db.create_document") as mock_create,
            patch("extractor.surreal_db.clone_chunks"),
        ):
            mock_create.return_value = {"id": "doc:new-clone-uuid"}
            _clone_deduplicated_doc(request, existing_doc, "new_upload.pdf", "unique-legal-hash-12345")

            self.assertTrue(mock_create.called)
            created_data = mock_create.call_args[0][0]
            self.assertEqual(created_data["publisher"], "Oxford Academic Press")
            self.assertEqual(created_data["publication_year"], "2026")
            self.assertEqual(created_data["license_type"], "CC-BY-4.0")
            self.assertEqual(created_data["doi"], "10.1093/oxford/2026.01")


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
        original_hosts = list(settings.ALLOWED_HOSTS)

        try:
            factory = RequestFactory()
            # Simulate a request from a custom domain
            request = factory.post(
                "/",
                HTTP_HOST="custom-tunnel-domain.example.com",
                HTTP_ORIGIN="https://custom-tunnel-domain.example.com",
            )

            # Ensure DEBUG is True and ALLOWED_HOSTS permits test domain
            with self.settings(
                DEBUG=True, ALLOWED_HOSTS=["custom-tunnel-domain.example.com", "localhost", "127.0.0.1"]
            ):
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
            settings.ALLOWED_HOSTS = original_hosts

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

    def test_dynamic_csrf_middleware_db_error_path(self):
        """Verify middleware gracefully handles database query errors without throwing exceptions."""
        from unittest.mock import patch

        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        DynamicCsrfTrustedOriginsMiddleware._db_origins_loaded = False
        DynamicCsrfTrustedOriginsMiddleware._last_query_time = 0.0

        factory = RequestFactory()
        request = factory.get("/")
        middleware = DynamicCsrfTrustedOriginsMiddleware(lambda req: "ok")

        with patch("extractor.models.SystemSettings.get_settings", side_effect=RuntimeError("DB unreachable")):
            response = middleware(request)
            self.assertEqual(response, "ok")

    def test_csrf_middleware_malformed_referer(self):
        """Verify _patched_process_view handles malformed referer URLs without crashing."""
        from django.middleware.csrf import CsrfViewMiddleware

        factory = RequestFactory()
        request = factory.post("/", HTTP_REFERER="http://[invalid-ipv6-host")
        csrf_mw = CsrfViewMiddleware(lambda req: None)

        def dummy_view(req):
            return None

        # Should not raise exception
        csrf_mw.process_view(request, dummy_view, (), {})

    def test_csrf_middleware_is_loopback_variations(self):
        """Verify _is_loopback handles empty, IPv6, and non-loopback inputs."""
        from core.middleware import DynamicCsrfTrustedOriginsMiddleware

        mw = DynamicCsrfTrustedOriginsMiddleware(lambda req: None)
        self.assertFalse(mw._is_loopback(""))
        self.assertFalse(mw._is_loopback("https://attacker.example.com"))
        self.assertTrue(mw._is_loopback("localhost:8000"))
        self.assertTrue(mw._is_loopback("127.0.0.1:8000"))
        self.assertTrue(mw._is_loopback("http://[::1]:8000"))


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
        self.client.force_login(self.normal_user)
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        logs = response.context["logs"]
        # Normal users should only see their own logs (the UPLOAD log + the LOGIN log)
        self.assertEqual(len(logs), 2)
        for log in logs:
            self.assertEqual(log.user, self.normal_user)

    def test_audit_logs_list_staff_user_all(self):
        self.client.force_login(self.staff_user)
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        logs = response.context["logs"]
        # Staff/Superusers should see all logs (the normal user upload, staff delete, plus the login logs)
        self.assertEqual(len(logs), 3)

    def test_audit_logs_filtering(self):
        self.client.force_login(self.staff_user)

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

    def test_audit_action_filter_normalizes_legacy_case_and_renders_local_time_markup(self):
        legacy_log = AuditLog.objects.create(
            user=self.staff_user,
            action="delete",
            details="Legacy delete record",
            ip_address="10.0.0.2",
        )
        self.client.force_login(self.staff_user)

        response = self.client.get(reverse("audit_logs") + "?action=delete")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["selected_action"], AuditAction.DELETE)
        self.assertEqual(len(response.context["logs"]), 2)
        self.assertIn(legacy_log, response.context["logs"])
        self.assertTrue(all(log.action.lower() == "delete" for log in response.context["logs"]))
        self.assertContains(response, 'class="doc-meta-sub local-datetime"')
        self.assertContains(response, 'datetime="')

    def test_audit_username_filter_is_not_rendered_for_standard_users(self):
        self.client.force_login(self.normal_user)

        response = self.client.get(reverse("audit_logs"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, 'id="user-filter"')

    def test_audit_logs_dangling_document(self):
        log_dangling = AuditLog.objects.create(
            user=self.normal_user,
            action=AuditAction.EXTRACTION_COMPLETED,
            document=self.doc1,
            details="Completed extraction for doc1",
            ip_address="127.0.0.1",
        )
        self.doc1.delete()
        log_dangling.refresh_from_db()
        self.assertIsNone(log_dangling.document)

        self.client.force_login(self.normal_user)
        response = self.client.get(reverse("audit_logs"))
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"--", response.content)


class StableSupabaseIdentityTestCase(TestCase):
    """Production ownership must not depend on a Cloud Run instance's local PK."""

    def test_request_actor_id_prefers_the_supabase_subject_in_production(self):
        user = User.objects.create_user(username="stable-owner", password="password123")
        request = RequestFactory().get("/")
        request.user = user
        request.session = {"supabase_user_id": "c955747c-322f-4e92-b933-5bca794710b3"}

        with self.settings(SURREALDB_OFFLINE=False):
            self.assertEqual(get_request_actor_id(request), "c955747c-322f-4e92-b933-5bca794710b3")

    def test_request_actor_id_keeps_local_id_for_explicit_offline_mode(self):
        user = User.objects.create_user(username="offline-owner", password="password123")
        request = RequestFactory().get("/")
        request.user = user
        request.session = {"supabase_user_id": "c955747c-322f-4e92-b933-5bca794710b3"}

        with self.settings(SURREALDB_OFFLINE=True):
            self.assertEqual(get_request_actor_id(request), str(user.id))

    @patch("extractor.surreal_db.log_audit")
    def test_audit_event_writes_the_stable_actor_id(self, mock_log_audit):
        user = User.objects.create_user(username="audit-owner", password="password123")

        log_audit_event(AuditEvent(action=AuditAction.LOGIN, user=user, actor_id="stable-supabase-subject"))

        self.assertEqual(mock_log_audit.call_args.kwargs["user_id"], "stable-supabase-subject")

    def test_surreal_audit_metadata_is_rendered_as_details(self):
        audit_log = _parse_surreal_audit_log(
            {
                "id": "audit:one",
                "timestamp": "2026-08-12T02:00:00Z",
                "action": AuditAction.LOGIN,
                "metadata": "Login succeeded",
            },
            {},
        )

        self.assertEqual(audit_log.details, "Login succeeded")

    def test_surreal_audit_details_support_structured_and_serialized_metadata(self):
        self.assertEqual(
            _parse_surreal_audit_details({"metadata": {"details": "Structured event"}}), "Structured event"
        )
        self.assertEqual(
            _parse_surreal_audit_details({"metadata": '{"details": "Serialized event"}'}), "Serialized event"
        )

    @patch("extractor.views._build_users_map")
    @patch("extractor.surreal_db._run")
    @patch("extractor.surreal_db._first_result")
    @patch("extractor.surreal_db.get_document")
    @patch("extractor.surreal_db.get_documents")
    def test_surreal_audit_logs_batch_fetches_documents(
        self, mock_get_docs, mock_get_doc, mock_first_result, mock_run, mock_users_map
    ):
        from extractor.views import _get_surreal_audit_logs

        user = User.objects.create_user(username="batch-audit-user", password="password123")
        request = RequestFactory().get("/")
        request.user = user
        request.session = {}

        raw_logs = [
            {
                "id": f"audit:{i}",
                "timestamp": "2026-08-12T02:00:00Z",
                "action": "UPLOAD",
                "user_id": str(user.id),
                "doc_uuid": f"00000000-0000-0000-0000-00000000000{i % 5}",
                "details": f"Details {i}",
                "ip_address": "127.0.0.1",
            }
            for i in range(20)
        ]
        mock_run.return_value = [raw_logs]
        mock_first_result.return_value = raw_logs
        mock_users_map.return_value = {str(user.id): user}
        mock_get_docs.return_value = [
            {
                "doc_uuid": f"00000000-0000-0000-0000-00000000000{i}",
                "title": f"Doc {i}",
                "original_filename": f"file_{i}.pdf",
                "status": "COMPLETED",
            }
            for i in range(5)
        ]

        result = _get_surreal_audit_logs(request, True, "", "", "")

        self.assertEqual(len(result["logs"]), 20)
        mock_get_docs.assert_called_once()
        mock_get_doc.assert_not_called()


class DeploymentControllerViewTestCase(TestCase):
    """Verifies DeploymentControllerView behaviour under various roles and scaling options."""

    def setUp(self):
        self.admin = User.objects.create_superuser(username="depadmin", password="password123")
        self.staff = User.objects.create_user(username="depstaff", password="password123", is_staff=True)
        self.user = User.objects.create_user(username="depuser", password="password123")
        self.client.force_login(self.admin)

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
        self.client.force_login(self.user)
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
                            "autoscaling.knative.dev/minScale": "1" if service == "korda-web" else "0",
                            "autoscaling.knative.dev/maxScale": "5" if service == "korda-web" else "3",
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
        self.assertEqual(response.context["worker_max"], 5)
        self.assertEqual(response.context["web_min"], 1)
        self.assertEqual(response.context["web_max"], 5)
        self.assertEqual(response.context["current_mode"], "on-demand")
        self.assertTrue(any("Could not load worker config from GCP" in message for message in log_capture.output))
        self.assertTrue(any("Could not load web config from GCP" in message for message in log_capture.output))

    def test_post_hibernate(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "hibernate"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("korda-worker", 0, 1)

    def test_post_on_demand(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "on-demand"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("korda-worker", 0, 5)

    def test_post_always_on(self):
        response = self.client.post(reverse("deployment_controller"), {"mode": "always-on"})
        self.assertRedirects(response, reverse("deployment_controller"))
        self.mocks[5].assert_called_once_with("korda-worker", 1, 5)

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

    def test_dashboard_stats_and_document_list_are_user_isolated(self):
        self.client.force_login(self.user_a)

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
        self.client.force_login(self.user_a)

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
            self.assertIn(str(self.doc_a.id), source_ids)
            self.assertNotIn(str(self.doc_b.id), source_ids)

    @patch("extractor.rag.generate_llm_content_unified")
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback")
    @patch("extractor.surreal_db.search_rag_cache_hnsw")
    def test_rag_search_semantic_cache_hit_returns_hydrated_sources(
        self, mock_search_cache, mock_execute, mock_generate
    ):
        self.client.force_login(self.user_a)

        mock_emb_val = MagicMock()
        mock_emb_val.values = [1.0, 0.0] * 384
        mock_query_resp = MagicMock()
        mock_query_resp.embeddings = [mock_emb_val]
        mock_execute.return_value = mock_query_resp

        # Return a semantic cache hit containing raw UUID strings in 'sources'
        mock_search_cache.return_value = [
            {
                "answer_text": "Cached answer from semantic cache.",
                "sources": [str(self.doc_a.uuid)],
                "score": 0.05,
            }
        ]

        with self.settings(GEMINI_API_KEY="mock-api-key"):
            response = self.client.get(reverse("rag_search"), {"q": "cached-query"})
            self.assertEqual(response.status_code, 200)
            data = response.json()

            self.assertEqual(data["answer"], "Cached answer from semantic cache.")
            self.assertTrue(len(data["sources"]) > 0)
            src = data["sources"][0]
            # Ensure the raw UUID string was hydrated into full dictionary schema
            self.assertIsInstance(src, dict)
            self.assertEqual(src["uuid"], str(self.doc_a.uuid))
            self.assertIn("title", src)
            self.assertIn("language", src)
            self.assertIn("chunk_index", src)
            self.assertIn("deep_link", src)
            self.assertIn(f"/document/{self.doc_a.uuid}/", src["deep_link"])

    def test_export_sqlite_view(self):
        from django.core.cache import cache

        cache.clear()
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("export_sqlite"), {"selected_documents": [self.doc_a.id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/x-sqlite3")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertTrue(len(response.content) > 0)

    def test_export_csv_view(self):
        from django.core.cache import cache

        cache.clear()
        self.client.force_login(self.user_a)
        response = self.client.post(reverse("export_csv"), {"selected_documents": [self.doc_a.id]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        self.assertIn("attachment; filename=", response["Content-Disposition"])
        self.assertIn(self.doc_a.title, response.content.decode("utf-8"))


class SecurityAuthTestCase(TestCase):
    """Verifies that various authentication, registration, recovery, and settings input checks are secure."""

    def assert_supabase_captcha_payload(self, mock_urlopen):
        import json

        request_body = json.loads(mock_urlopen.call_args.args[0].data.decode("utf-8"))
        self.assertNotIn("captcha_token", request_body)
        self.assertEqual(request_body["gotrue_meta_security"], {"captcha_token": "valid-captcha-token"})

    @patch("urllib.request.urlopen")
    def test_registration_sends_captcha_in_gotrue_security_metadata(self, mock_urlopen):
        from extractor.views import _register_supabase_user

        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        success, error_message = _register_supabase_user(
            "https://project.supabase.co",
            "mock-public-key",
            "test@example.com",
            "password123",
            "https://app.example.com",
            "valid-captcha-token",
        )

        self.assertTrue(success)
        self.assertIsNone(error_message)
        self.assert_supabase_captcha_payload(mock_urlopen)

    @patch("urllib.request.urlopen")
    def test_recovery_sends_captcha_in_gotrue_security_metadata(self, mock_urlopen):
        from extractor.views import _send_supabase_recovery

        mock_response = MagicMock()
        mock_response.__enter__.return_value = mock_response
        mock_urlopen.return_value = mock_response

        success, error_message = _send_supabase_recovery(
            "test@example.com",
            "https://project.supabase.co",
            "mock-public-key",
            "https://app.example.com",
            "valid-captcha-token",
        )

        self.assertTrue(success)
        self.assertIsNone(error_message)
        self.assert_supabase_captcha_payload(mock_urlopen)

    def test_login_requires_turnstile_token_when_configured(self):
        with self.settings(CF_TURNSTILE_SITE_KEY="test-site-key"):
            response = self.client.post(
                reverse("login"),
                {"username": "test@example.com", "password": "password123"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CAPTCHA verification is required")

    def test_login_turnstile_guard_is_non_blocking_for_the_submit_button(self):
        with self.settings(CF_TURNSTILE_SITE_KEY="test-site-key"):
            response = self.client.get(reverse("login"))

        self.assertContains(response, "showTurnstileValidationError")
        self.assertContains(response, "e.defaultPrevented")
        self.assertNotContains(response, "Please complete the CAPTCHA verification before submitting.")

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

    def test_login_page_renders_turnstile_widget_when_configured(self):
        with self.settings(CF_TURNSTILE_SITE_KEY="test-site-key"):
            response = self.client.get(reverse("login"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "https://challenges.cloudflare.com/turnstile/v0/api.js")
            self.assertContains(response, 'data-sitekey="test-site-key"')

    def test_register_page_renders_turnstile_widget_when_configured(self):
        with self.settings(CF_TURNSTILE_SITE_KEY="test-site-key"):
            response = self.client.get(reverse("register"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "https://challenges.cloudflare.com/turnstile/v0/api.js")
            self.assertContains(response, 'data-sitekey="test-site-key"')

    @patch("urllib.request.urlopen")
    def test_supabase_app_metadata_admin_promotion(self, mock_urlopen):
        import json

        from extractor.auth import SupabaseAuthBackend

        # User has app_metadata.is_admin = True
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps(
            {"user": {"id": "mock-uuid-2", "email": "promoted_user@example.com", "app_metadata": {"is_admin": True}}}
        ).encode("utf-8")
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="",
            DEBUG=False,
        ):
            user = backend.authenticate(None, username="promoted_user@example.com", password="password123")

            self.assertIsNotNone(user)
            self.assertTrue(user.is_superuser)
            self.assertTrue(user.is_staff)

    @patch("urllib.request.urlopen")
    def test_first_user_is_not_automatically_promoted(self, mock_urlopen):
        import json

        from django.contrib.auth.models import User

        from extractor.auth import SupabaseAuthBackend

        # An empty local database must not turn the first public signup into an administrator.
        User.objects.all().delete()

        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps(
            {"user": {"id": "mock-uuid-3", "email": "first_user@example.com"}}
        ).encode("utf-8")
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="",
            DEBUG=False,
        ):
            user = backend.authenticate(None, username="first_user@example.com", password="password123")

            self.assertIsNotNone(user)
            self.assertFalse(user.is_superuser)
            self.assertFalse(user.is_staff)

    def test_static_example_email_is_not_an_admin_grant(self):
        from extractor.auth import _sync_supabase_user

        with self.settings(ADMIN_EMAIL=""):
            user = _sync_supabase_user(None, {"user": {"email": "admin@example.com"}}, "admin@example.com")

        self.assertFalse(user.is_superuser)
        self.assertFalse(user.is_staff)

    def test_forgot_password_page_renders_turnstile_widget_when_configured(self):
        with self.settings(CF_TURNSTILE_SITE_KEY="test-site-key"):
            response = self.client.get(reverse("forgot_password"))
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "https://challenges.cloudflare.com/turnstile/v0/api.js")
            self.assertContains(response, 'data-sitekey="test-site-key"')
            self.assertContains(response, 'id="forgot-form"')

    def test_forgot_password_requires_turnstile_token_when_configured(self):
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="test-site-key",
        ):
            response = self.client.post(reverse("forgot_password"), {"email": "test@example.com"})
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "CAPTCHA verification is required")

    def test_register_requires_turnstile_token_when_configured(self):
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="test-site-key",
        ):
            response = self.client.post(
                reverse("register"),
                {"email": "test@example.com", "password": "password123", "confirm_password": "password123"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertContains(response, "CAPTCHA verification is required")

    @patch("urllib.request.urlopen")
    def test_supabase_admin_promotion_security(self, mock_urlopen):
        # Mock successful login response from Supabase for a normal user email but trying to log in as "admin"
        import json

        from django.contrib.auth.models import User

        from extractor.auth import SupabaseAuthBackend

        # Create an initial superuser so that the auto-admin bootstrapping does not trigger for the normal user
        User.objects.create_superuser(username="existing_admin", email="existing_admin@example.com", password="pwd")

        # User email is not admin@<domain>
        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps(
            {"user": {"id": "mock-uuid-1", "email": "normal_user@example.com"}}
        ).encode("utf-8")
        mock_urlopen.return_value = mock_resp

        backend = SupabaseAuthBackend()
        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            CF_TURNSTILE_SITE_KEY="",
            DEBUG=False,
        ):
            # Pass email as username
            user = backend.authenticate(None, username="normal_user@example.com", password="password123")

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
        self.client.force_login(self.user)

        response = self.client.post(
            reverse("bulk_action"),
            {
                "action": "restart",
                "selected_documents": [str(self.doc1.uuid), str(self.doc2.uuid)],
            },
        )
        self.assertEqual(response.status_code, 302)

        self.doc1.refresh_from_db()
        self.doc2.refresh_from_db()

        # Verify status reset to PENDING and metrics reset to zero
        self.assertEqual(self.doc1.status, "PENDING")
        self.assertEqual(self.doc2.status, "PENDING")
        self.assertEqual(self.doc1.retry_count, 0)
        self.assertEqual(self.doc2.retry_count, 0)
        self.assertEqual(float(self.doc1.cost_usd), 0.0)
        self.assertEqual(self.doc1.input_tokens, 0)
        self.assertEqual(self.doc1.output_tokens, 0)

        # Verify audit logs created
        self.assertTrue(AuditLog.objects.filter(action=AuditAction.DOCUMENT_REQUEUED, user=self.user).exists())

    @patch("django.core.files.storage.default_storage.exists", return_value=False)
    @patch("django.core.files.storage.default_storage.delete")
    @patch("extractor.surreal_db.delete_chunks")
    def test_bulk_delete(self, mock_delete_chunks, mock_storage_delete, mock_storage_exists):
        self.client.force_login(self.user)

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

    @patch("django.core.files.storage.default_storage.exists", return_value=False)
    @patch("django.core.files.storage.default_storage.delete")
    @patch("extractor.surreal_db.delete_chunks")
    def test_bulk_delete_query_efficiency_and_deduplication(
        self, mock_delete_chunks, mock_storage_delete, mock_storage_exists
    ):
        self.client.force_login(self.user)

        # Create 10 documents with the same file hash
        docs = []
        for i in range(10):
            docs.append(
                SourceDocument.objects.create(
                    original_filename=f"doc_same_{i}.pdf",
                    file_hash="shared_hash_123",
                    title=f"Doc Same {i}",
                    status="COMPLETED",
                    uploaded_by=self.user,
                )
            )

        doc_ids = [d.id for d in docs]

        with self.assertNumQueries(37):
            response = self.client.post(
                reverse("bulk_action"),
                {
                    "action": "delete",
                    "selected_documents": doc_ids,
                },
            )
            self.assertEqual(response.status_code, 302)

        # Verify they are all deleted from DB
        self.assertEqual(SourceDocument.objects.filter(id__in=doc_ids).count(), 0)


class CoreDesignHardeningTests(TestCase):
    def setUp(self):
        self.username = "test_user_hardening"
        self.email = "test_hardening@example.com"
        self.password = "Secr3tPass!"
        self.user = User.objects.create_user(username=self.username, email=self.email, password=self.password)
        self.client.force_login(self.user)

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
        mock_rag.assert_called_once_with(
            "filtered-query", document_ids=[5, 12], top_k=5, user=self.user, actor_id=str(self.user.id)
        )


class DatetimeUtilityTestCase(TestCase):
    """Verifies robustness of parse_datetime utility function to prevent regressions."""

    def test_parse_datetime_iso_string(self):
        from datetime import datetime

        from django.utils import timezone

        from extractor.views import parse_datetime

        val = "2026-07-13T10:00:00Z"
        parsed = parse_datetime(val)
        self.assertIsInstance(parsed, datetime)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 7)
        self.assertEqual(parsed.day, 13)
        self.assertEqual(parsed.hour, 10)
        self.assertEqual(parsed.tzinfo, timezone.UTC)

    def test_parse_datetime_fallback_on_invalid(self):
        from datetime import datetime

        from extractor.views import parse_datetime

        parsed = parse_datetime("invalid-date-format-string")
        self.assertIsInstance(parsed, datetime)

        parsed_none = parse_datetime(None)
        self.assertIsInstance(parsed_none, datetime)


class SupabaseSessionExchangeTestCase(TestCase):
    """Verifies Supabase client-side OAuth session exchange view."""

    def test_missing_access_token(self):
        import json

        response = self.client.post(
            reverse("supabase_session_exchange"),
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("error", data)

    def test_unconfigured_supabase(self):
        import json

        with self.settings(SUPABASE_URL="", SUPABASE_PUBLIC_KEY=""):
            response = self.client.post(
                reverse("supabase_session_exchange"),
                data=json.dumps({"access_token": "mock-token"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 503)

    @patch("urllib.request.urlopen")
    def test_successful_session_exchange(self, mock_urlopen):
        import json

        mock_resp = MagicMock()
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.read.return_value = json.dumps({"id": "oauth-uuid-123", "email": "oauth.user@example.com"}).encode(
            "utf-8"
        )
        mock_urlopen.return_value = mock_resp

        with self.settings(
            SUPABASE_URL="https://project.supabase.co",
            SUPABASE_PUBLIC_KEY="mock-public-key",
            ADMIN_EMAIL="",
        ):
            response = self.client.post(
                reverse("supabase_session_exchange"),
                data=json.dumps({"access_token": "valid-mock-jwt"}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 200)
            data = response.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["user"], "oauth.user")
            self.assertEqual(self.client.session.get("supabase_user_id"), "oauth-uuid-123")

    def test_supabase_session_exchange_malformed_json_guard(self):
        """Verify SupabaseSessionExchangeView returns 400 on malformed JSON payload."""
        response = self.client.post(
            reverse("supabase_session_exchange"),
            data="not a valid json string {",
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertEqual(data.get("error"), "Malformed JSON payload.")


class ViewsExceptionPathsTestCase(TestCase):
    """Direct unit tests for helper functions and exception paths in views.py."""

    def test_parse_datetime_variations(self):
        from datetime import datetime

        from django.utils import timezone

        from extractor.views import parse_datetime

        now = timezone.now()
        # datetime object returned directly
        self.assertEqual(parse_datetime(now), now)

        # None/empty returns current time
        self.assertIsInstance(parse_datetime(None), datetime)
        self.assertIsInstance(parse_datetime(""), datetime)

        # ISO format
        dt_str = "2026-08-15T12:00:00Z"
        parsed = parse_datetime(dt_str)
        self.assertEqual(parsed.year, 2026)
        self.assertEqual(parsed.month, 8)
        self.assertEqual(parsed.day, 15)

        # Django parse alternative format
        alt_str = "2026-08-15 14:30:00"
        parsed_alt = parse_datetime(alt_str)
        self.assertEqual(parsed_alt.hour, 14)

        # Completely invalid string returns current time
        self.assertIsInstance(parse_datetime("invalid-date-string-xyz"), datetime)

    def test_supabase_register_error_handling(self):
        import io
        import json
        import urllib.error
        from unittest.mock import patch

        from extractor.views import _register_supabase_user

        # Test HTTPError with JSON error body
        err_json = json.dumps({"msg": "User already registered"}).encode("utf-8")
        http_err = urllib.error.HTTPError("http://supabase", 400, "Bad Request", {}, io.BytesIO(err_json))
        with patch("urllib.request.urlopen", side_effect=http_err):
            success, msg = _register_supabase_user(
                "https://sub.supabase.co", "key", "test@example.com", "pass", "http://app", ""
            )
            self.assertFalse(success)
            self.assertIn("User already registered", msg)

        # Test HTTPError with non-JSON body
        non_json_err = urllib.error.HTTPError(
            "http://supabase", 500, "Server Error", {}, io.BytesIO(b"Internal failure")
        )
        with patch("urllib.request.urlopen", side_effect=non_json_err):
            success, msg = _register_supabase_user(
                "https://sub.supabase.co", "key", "test@example.com", "pass", "http://app", ""
            )
            self.assertFalse(success)
            self.assertIn("Internal failure", msg)

        # Test generic Exception
        with patch("urllib.request.urlopen", side_effect=ConnectionResetError("Connection lost")):
            success, msg = _register_supabase_user(
                "https://sub.supabase.co", "key", "test@example.com", "pass", "http://app", ""
            )
            self.assertFalse(success)
            self.assertIn("Connection lost", msg)

    def test_supabase_recovery_error_handling(self):
        import io
        import json
        import urllib.error
        from unittest.mock import patch

        from extractor.views import _send_supabase_recovery

        # Test HTTPError with JSON error body
        err_json = json.dumps({"error_description": "Rate limit exceeded"}).encode("utf-8")
        http_err = urllib.error.HTTPError("http://supabase", 429, "Too Many Requests", {}, io.BytesIO(err_json))
        with patch("urllib.request.urlopen", side_effect=http_err):
            success, msg = _send_supabase_recovery(
                "test@example.com", "https://sub.supabase.co", "key", "http://app", ""
            )
            self.assertFalse(success)
            self.assertIn("Rate limit exceeded", msg)

        # Test generic Exception
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
            success, msg = _send_supabase_recovery(
                "test@example.com", "https://sub.supabase.co", "key", "http://app", ""
            )
            self.assertFalse(success)
            self.assertIn("timed out", msg)

    def test_multilabel_and_idna_email_validation(self):
        from extractor.views import _validate_registration_inputs

        valid_emails = [
            "user@mail.example.com",
            "user@example.xn--p1ai",
            "first.last+tag@sub.domain.org",
        ]
        for email in valid_emails:
            err = _validate_registration_inputs(
                email=email,
                password="SecurePassword123!",
                confirm_password="SecurePassword123!",
                supabase_url="https://test.supabase.co",
                supabase_key="secret-key",
            )
            self.assertIsNone(err, f"Expected {email} to pass registration validation")

        invalid_emails = [
            "user@",
            "@example.com",
            "user@.com",
            "invalid email@example.com",
        ]
        for email in invalid_emails:
            err = _validate_registration_inputs(
                email=email,
                password="SecurePassword123!",
                confirm_password="SecurePassword123!",
                supabase_url="https://test.supabase.co",
                supabase_key="secret-key",
            )
            self.assertEqual(err, "Invalid email format.")

    def test_save_settings_csrf_origin_format_validation(self):
        """Verify SaveSettingsView rejects origins without http:// or https:// scheme."""
        user = User.objects.create_superuser(
            username="settings_admin", password="Password123!", email="admin@example.com"
        )
        self.client.force_login(user)
        # Invalid origin without scheme
        response = self.client.post(
            reverse("save_settings"),
            {
                "csrf_trusted_origins": "example.com, https://valid.com",
                "monthly_budget_usd": "20.00",
                "currency": "USD",
                "selected_model": "auto",
            },
        )
        self.assertEqual(response.status_code, 302)
        # Verify invalid origin was rejected and settings remain default
        settings_obj = SystemSettings.get_settings()
        self.assertNotEqual(settings_obj.csrf_trusted_origins, "example.com, https://valid.com")

        # Valid HTTPS origins (multiline and comma-separated)
        response = self.client.post(
            reverse("save_settings"),
            {
                "csrf_trusted_origins": "https://example.com, https://localhost:3000\nhttps://sub.domain.org",
                "monthly_budget_usd": "25.00",
                "currency": "USD",
                "selected_model": "auto",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings_obj = SystemSettings.get_settings()
        self.assertEqual(
            settings_obj.csrf_trusted_origins,
            "https://example.com, https://localhost:3000\nhttps://sub.domain.org",
        )

    def test_document_retry_conflict_on_in_flight_document(self):
        """Verify DocumentRetryView rejects in-flight documents with 409 Conflict."""
        user = User.objects.create_user(username="retry_tester", password="Password123!")
        self.client.force_login(user)
        in_flight_doc = SourceDocument.objects.create(
            original_filename="inflight.pdf",
            file_hash="inflight-hash-409",
            title="In Flight Doc",
            status="EXTRACTING",
            uploaded_by=user,
        )
        response = self.client.post(
            reverse("retry_document", kwargs={"doc_uuid": in_flight_doc.uuid}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 409)
        data = response.json()
        self.assertIn("being processed", data.get("error", "").lower())

    def test_bulk_delete_with_uuid_strings_offline(self):
        """Verify bulk delete and _get_docs_for_delete handle string UUIDs in offline mode."""
        from extractor.views import _get_docs_for_delete

        user = User.objects.create_user(username="bulk_del_tester", password="Password123!")
        doc = SourceDocument.objects.create(
            original_filename="bulk_uuid.pdf",
            file_hash="bulk-uuid-hash-999",
            title="Bulk UUID Doc",
            status="COMPLETED",
            uploaded_by=user,
        )
        request = MagicMock()
        request.user = user

        with self.settings(SURREALDB_OFFLINE=True):
            resolved = _get_docs_for_delete(request, [str(doc.uuid)])
            self.assertEqual(len(resolved), 1)
            self.assertEqual(resolved[0].id, doc.id)

    def test_filter_audit_logs_metadata_search_fields(self):
        """Verify _filter_audit_logs correctly filters by document metadata (doi, publisher, year)."""
        from types import SimpleNamespace

        from extractor.views import _filter_audit_logs

        doc = SimpleNamespace(
            original_filename="sample_paper.pdf",
            title="Deep Learning in Biology",
            publisher="Nature Publishing",
            publication_year="2026",
            doi="10.1038/s41586-026-0001",
        )
        log1 = SimpleNamespace(
            action="UPLOAD",
            details="Uploaded file sample_paper.pdf",
            ip_address="127.0.0.1",
            document=doc,
            user=None,
        )
        logs = [log1]

        # Search by DOI
        filtered_doi = _filter_audit_logs(logs, False, "", "", "10.1038")
        self.assertEqual(len(filtered_doi), 1)

        # Search by Publisher
        filtered_pub = _filter_audit_logs(logs, False, "", "", "nature")
        self.assertEqual(len(filtered_pub), 1)

        # Search by Year
        filtered_year = _filter_audit_logs(logs, False, "", "", "2026")
        self.assertEqual(len(filtered_year), 1)

        # Non-matching search
        filtered_none = _filter_audit_logs(logs, False, "", "", "nonexistent-query")
        self.assertEqual(len(filtered_none), 0)

    @patch("extractor.rag.stream_query_rag")
    def test_stream_query_rag_view(self, mock_stream):
        user = User.objects.create_user(username="stream_tester", password="Password123!")
        self.client.force_login(user)

        def fake_generator(*args, **kwargs):
            yield 'data: {"sources": []}\n\n'
            yield 'data: {"token": "Hello"}\n\n'
            yield 'data: {"done": true}\n\n'

        mock_stream.side_effect = fake_generator

        response = self.client.get(reverse("stream_query_rag") + "?q=TestQuery")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        content = b"".join(response.streaming_content).decode("utf-8")
        self.assertIn("Hello", content)
        self.assertIn('"done": true', content)

    @patch("extractor.cloud_tasks.enqueue")
    def test_document_retry_pending_document_succeeds(self, mock_enqueue):
        """Verify DocumentRetryView allows restarting documents stuck in PENDING status and resets retry_count."""
        user = User.objects.create_user(username="pending_retry_tester", password="Password123!")
        self.client.force_login(user)
        pending_doc = SourceDocument.objects.create(
            original_filename="pending_stuck.pdf",
            file_hash="pending-hash-123",
            title="Pending Stuck Doc",
            status="PENDING",
            uploaded_by=user,
        )
        response = self.client.post(
            reverse("retry_document", kwargs={"doc_uuid": pending_doc.uuid}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        pending_doc.refresh_from_db()
        self.assertEqual(pending_doc.retry_count, 0)
        mock_enqueue.assert_called_once()

    def test_document_delete_ajax_json_response(self):
        """Verify DocumentDeleteView returns JSON response for AJAX requests."""
        user = User.objects.create_user(username="delete_ajax_tester", password="Password123!")
        self.client.force_login(user)
        doc = SourceDocument.objects.create(
            original_filename="delete_me.pdf",
            file_hash="delete-hash-456",
            title="Delete Me",
            status="FAILED",
            uploaded_by=user,
        )
        response = self.client.post(
            reverse("delete_document", kwargs={"doc_uuid": doc.uuid}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        self.assertIn("deleted successfully", data.get("message", ""))
        self.assertFalse(SourceDocument.objects.filter(uuid=doc.uuid).exists())

    def test_document_cancel_terminal_state_rejected(self):
        """Verify DocumentCancelView rejects cancelling a completed document."""
        user = User.objects.create_user(username="cancel_term_tester", password="Password123!")
        self.client.force_login(user)
        doc = SourceDocument.objects.create(
            original_filename="completed.pdf",
            file_hash="term-hash-123",
            title="Completed Doc",
            status="COMPLETED",
            uploaded_by=user,
        )
        response = self.client.post(
            reverse("cancel_document", kwargs={"doc_uuid": doc.uuid}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 400)
        data = response.json()
        self.assertIn("Cannot cancel document in terminal state", data.get("error", ""))

    @patch("extractor.views.broadcast_status_change")
    def test_document_cancel_inflight_success(self, mock_broadcast):
        """Verify DocumentCancelView cancels an in-flight extracting document."""
        user = User.objects.create_user(username="cancel_inflight_tester", password="Password123!")
        self.client.force_login(user)
        doc = SourceDocument.objects.create(
            original_filename="inflight.pdf",
            file_hash="inflight-hash-123",
            title="Inflight Doc",
            status="EXTRACTING",
            uploaded_by=user,
        )
        response = self.client.post(
            reverse("cancel_document", kwargs={"doc_uuid": doc.uuid}),
            HTTP_X_REQUESTED_WITH="XMLHttpRequest",
        )
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data.get("status"), "success")
        mock_broadcast.assert_called_once_with(str(doc.uuid), "FAILED")

    def test_reset_password_confirm_cache_headers(self):
        """Verify reset_password_confirm view has no-cache headers."""
        response = self.client.get(reverse("reset_password_confirm"))
        self.assertEqual(response.status_code, 200)
        cache_control = response.get("Cache-Control", "")
        self.assertIn("no-cache", cache_control)
