from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase

from core.database import database_config_from_url


class DatabaseConfigTests(SimpleTestCase):
    def test_postgres_uri_uses_transaction_pool_safe_settings(self):
        config = database_config_from_url(
            "postgresql://app_user:encoded%2Fpassword@pooler.example.com:6543/app?sslmode=require"
        )

        self.assertEqual(config["ENGINE"], "django.db.backends.postgresql")
        self.assertEqual(config["NAME"], "app")
        self.assertEqual(config["USER"], "app_user")
        self.assertEqual(config["PASSWORD"], "encoded/password")
        self.assertEqual(config["PORT"], "6543")
        self.assertEqual(config["OPTIONS"], {"sslmode": "require"})
        self.assertEqual(config["CONN_MAX_AGE"], 0)
        self.assertTrue(config["DISABLE_SERVER_SIDE_CURSORS"])

    def test_non_postgres_uri_is_rejected(self):
        with self.assertRaises(ImproperlyConfigured):
            database_config_from_url("sqlite:///:memory:")

    def test_postgres_uri_defaults_to_required_tls(self):
        config = database_config_from_url("postgresql://app_user:password@pooler.example.com/app")

        self.assertEqual(config["OPTIONS"]["sslmode"], "require")

    def test_postgres_uri_rejects_insecure_tls_modes(self):
        for sslmode in ("disable", "allow", "prefer"):
            with self.subTest(sslmode=sslmode), self.assertRaises(ImproperlyConfigured):
                database_config_from_url(f"postgresql://app_user:password@pooler.example.com/app?sslmode={sslmode}")
