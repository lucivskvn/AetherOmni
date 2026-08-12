import os
from unittest.mock import patch

from django.apps import apps
from django.test import TestCase


class ExtractorConfigReadyTest(TestCase):
    def setUp(self) -> None:
        self.app_config = apps.get_app_config("extractor")

    @patch.dict(os.environ, {"RUN_MAIN": "true", "GUNICORN_WORKER": ""}, clear=False)
    @patch.object(type(apps.get_app_config("extractor")), "_start_periodic_cleanup")
    @patch.object(type(apps.get_app_config("extractor")), "_start_periodic_reaper")
    def test_ready_skips_django_autoreload_parent(self, start_reaper, start_cleanup):
        self.app_config.ready()

        start_reaper.assert_not_called()
        start_cleanup.assert_not_called()

    @patch.dict(os.environ, {"RUN_MAIN": "", "GUNICORN_WORKER": "1"}, clear=False)
    @patch.object(type(apps.get_app_config("extractor")), "_start_periodic_cleanup")
    @patch.object(type(apps.get_app_config("extractor")), "_start_periodic_reaper")
    def test_ready_skips_gunicorn_child_worker(self, start_reaper, start_cleanup):
        self.app_config.ready()

        start_reaper.assert_not_called()
        start_cleanup.assert_not_called()
