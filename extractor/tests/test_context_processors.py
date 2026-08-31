import os

from django.conf import settings
from django.test import RequestFactory, TestCase, override_settings

from extractor.context_processors import system_settings
from extractor.models import SystemSettings


class SystemSettingsContextProcessorTestCase(TestCase):
    """
    Tests the system_settings context processor to verify that the returned dictionary
    properly contains SystemSettings, Supabase credentials, and the Release Version.
    """

    def setUp(self):
        self.factory = RequestFactory()
        self.request = self.factory.get("/")

    @override_settings(SUPABASE_URL="https://test-supabase-url.co", SUPABASE_PUBLIC_KEY="test-pub-key-123")
    def test_happy_path(self):
        """
        Happy path: verify that when credentials and RELEASE_VERSION are set, they are correctly
        injected into the template context.
        """
        # Clear or set environment variables for deterministic testing
        with patch_environ({"RELEASE_VERSION": "v1.2.3", "COMMIT_SHA": "497a1f8"}):
            context = system_settings(self.request)

            # Check context keys and values
            self.assertIn("system_settings", context)
            self.assertIsInstance(context["system_settings"], SystemSettings)

            self.assertEqual(context["SUPABASE_URL"], "https://test-supabase-url.co")
            self.assertEqual(context["SUPABASE_PUBLIC_KEY"], "test-pub-key-123")
            self.assertEqual(context["RELEASE_VERSION"], "1.2.3")
            self.assertEqual(context["COMMIT_SHA"], "497a1f8")

    @override_settings(SUPABASE_URL="https://test-supabase-url.co", SUPABASE_PUBLIC_KEY="test-pub-key-123")
    def test_existing_settings_record(self):
        """
        Verify that if a SystemSettings record already exists, get_settings() retrieves
        it correctly, and any changes on the record are populated in the template context.
        """
        # Retrieve the singleton or create it, then update a field
        settings_obj = SystemSettings.get_settings()
        settings_obj.selected_model = "gemini-1.5-pro"
        settings_obj.save()

        context = system_settings(self.request)
        self.assertIn("system_settings", context)
        self.assertEqual(context["system_settings"].selected_model, "gemini-1.5-pro")

    @override_settings()
    def test_missing_settings(self):
        """
        Edge case: verify that when SUPABASE_URL or SUPABASE_PUBLIC_KEY is not defined on settings,
        it safely falls back to empty strings.
        """
        # override_settings() copies settings so they are automatically restored after test.
        if hasattr(settings, "SUPABASE_URL"):
            del settings.SUPABASE_URL
        if hasattr(settings, "SUPABASE_PUBLIC_KEY"):
            del settings.SUPABASE_PUBLIC_KEY

        context = system_settings(self.request)
        self.assertEqual(context["SUPABASE_URL"], "")
        self.assertEqual(context["SUPABASE_PUBLIC_KEY"], "")

    def test_missing_env_release_version(self):
        """
        Edge case: verify that when RELEASE_VERSION is missing from os.environ AND
        the VERSION file is not readable, the processor falls back to a valid
        semantic-version sentinel.
        """
        with patch_environ_deleted("RELEASE_VERSION"), override_settings(BASE_DIR="/nonexistent-path-for-test"):
            context = system_settings(self.request)
            self.assertEqual(context["RELEASE_VERSION"], "0.0.0")


class patch_environ:
    """Helper context manager to temporarily patch os.environ with test values."""

    def __init__(self, update_dict):
        self.update_dict = update_dict
        self.original_vals = {}

    def __enter__(self):
        for k, v in self.update_dict.items():
            self.original_vals[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self.original_vals.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class patch_environ_deleted:
    """Helper context manager to temporarily delete keys from os.environ."""

    def __init__(self, *keys):
        self.keys = keys
        self.original_vals = {}

    def __enter__(self):
        for k in self.keys:
            self.original_vals[k] = os.environ.get(k)
            os.environ.pop(k, None)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        for k, v in self.original_vals.items():
            if v is not None:
                os.environ[k] = v
