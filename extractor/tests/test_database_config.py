from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from core.settings import _database_config_for_runtime


class DatabaseRuntimeConfigurationTests(SimpleTestCase):
    def test_cloud_run_requires_a_database_url(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "DATABASE_URL must be configured"):
            _database_config_for_runtime("", is_cloud_run=True, offline=False, base_dir=Path("/app"))

    def test_cloud_run_rejects_offline_mode_even_when_database_url_is_set(self):
        with self.assertRaisesMessage(ImproperlyConfigured, "SURREALDB_OFFLINE cannot be enabled"):
            _database_config_for_runtime("postgresql://unused", is_cloud_run=True, offline=True, base_dir=Path("/app"))

    def test_local_offline_mode_uses_sqlite(self):
        databases = _database_config_for_runtime("", is_cloud_run=False, offline=True, base_dir=Path("/app"))

        self.assertEqual(databases["default"]["ENGINE"], "django.db.backends.sqlite3")
        self.assertEqual(databases["default"]["NAME"], Path("/app/db.sqlite3"))
