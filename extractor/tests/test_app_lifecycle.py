import os
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase, override_settings


class AsgiWsgiAppConfigTest(TestCase):
    def test_asgi_application_import(self):
        from core.asgi import application as asgi_app

        self.assertIsNotNone(asgi_app)

    def test_wsgi_application_import(self):
        from core.wsgi import application as wsgi_app

        self.assertIsNotNone(wsgi_app)

    @patch.dict(os.environ, {"RUN_MAIN": "", "GUNICORN_WORKER": ""}, clear=False)
    @override_settings(ENABLE_PERIODIC_MAINTENANCE=True)
    @patch("threading.Thread")
    def test_extractor_config_ready_scheduling(self, mock_thread):
        app_config = apps.get_app_config("extractor")

        # Simulate ready call when not in test or main bypass
        with patch("sys.argv", ["manage.py", "runserver"]):
            app_config.ready()

        self.assertTrue(mock_thread.called)
        self.assertEqual(mock_thread.call_count, 2)
