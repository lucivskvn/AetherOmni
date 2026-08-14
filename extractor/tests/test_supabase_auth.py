import json
import urllib.error
from unittest.mock import MagicMock, patch

from django.contrib.auth.models import User
from django.test import TestCase, override_settings

from extractor.auth import SupabaseAuthBackend


class SupabaseAuthBackendSecurityTests(TestCase):
    def setUp(self):
        self.email = "member@example.com"
        self.password = "Strong-password-123"
        self.user = User.objects.create_user(username="member", email=self.email, password=self.password)

    def _successful_response(self, is_admin):
        response = MagicMock()
        response.read.return_value = json.dumps(
            {
                "user": {
                    "id": "supabase-user-id",
                    "email": self.email,
                    "app_metadata": {"is_admin": is_admin},
                }
            }
        ).encode("utf-8")
        response.__enter__.return_value = response
        return response

    @override_settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="public-key")
    @patch("urllib.request.urlopen")
    def test_supabase_role_demotion_clears_django_admin_flags(self, mock_urlopen):
        self.user.is_superuser = True
        self.user.is_staff = True
        self.user.save()
        mock_urlopen.return_value = self._successful_response(is_admin=False)

        authenticated = SupabaseAuthBackend().authenticate(
            MagicMock(POST={}), username=self.email, password=self.password
        )

        self.assertEqual(authenticated.pk, self.user.pk)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_superuser)
        self.assertFalse(self.user.is_staff)

    @override_settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="public-key")
    @patch("urllib.request.urlopen")
    def test_configured_supabase_failure_never_falls_back_to_local_password(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("Supabase unavailable")

        authenticated = SupabaseAuthBackend().authenticate(
            MagicMock(POST={}), username=self.email, password=self.password
        )

        self.assertIsNone(authenticated)

    @override_settings(SUPABASE_URL="https://project.supabase.co", SUPABASE_PUBLIC_KEY="public-key")
    def test_configured_supabase_rejects_local_username_login(self):
        authenticated = SupabaseAuthBackend().authenticate(None, username=self.user.username, password=self.password)

        self.assertIsNone(authenticated)

    @override_settings(SUPABASE_URL="", SUPABASE_PUBLIC_KEY="")
    def test_unconfigured_offline_development_preserves_local_login(self):
        authenticated = SupabaseAuthBackend().authenticate(None, username=self.user.username, password=self.password)

        self.assertEqual(authenticated.pk, self.user.pk)
