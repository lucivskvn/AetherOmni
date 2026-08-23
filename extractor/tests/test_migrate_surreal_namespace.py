from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from scripts.migrate_surreal_namespace import SurrealNamespaceMigrator


class MigrateSurrealNamespaceTestCase(SimpleTestCase):
    """Unit tests verifying SurrealDB namespace migration logic and record reconciliation."""

    def setUp(self):
        self.migrator = SurrealNamespaceMigrator(
            surreal_url="http://localhost:8001",
            user="root",
            password="root",
            db_name="extractor",
            source_ns="aetheromni",
            target_ns="korda",
        )

    @patch("scripts.migrate_surreal_namespace.httpx.Client")
    def test_count_records(self, mock_client_cls):
        mock_client = MagicMock()
        mock_client.__enter__.return_value = mock_client
        mock_resp = MagicMock()
        mock_resp.json.return_value = [{"result": [{"count": 42}], "status": "OK"}]
        mock_resp.raise_for_status.return_value = None
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        count = self.migrator.count_records("aetheromni", "documents")
        self.assertEqual(count, 42)

    @patch.object(SurrealNamespaceMigrator, "count_records")
    @patch.object(SurrealNamespaceMigrator, "_execute_sql")
    def test_migrate_table_data(self, mock_exec, mock_count):
        # 10 records in source, 10 in destination after migration
        mock_count.side_effect = [2, 2]
        mock_exec.return_value = [
            {
                "result": [
                    {"id": "documents:doc1", "title": "Doc 1"},
                    {"id": "documents:doc2", "title": "Doc 2"},
                ],
                "status": "OK",
            }
        ]

        migrated = self.migrator.migrate_table_data("documents", batch_size=10, dry_run=False)
        self.assertEqual(migrated, 2)
        self.assertTrue(mock_exec.called)

    @patch.object(SurrealNamespaceMigrator, "count_records")
    def test_dry_run_does_not_execute_writes(self, mock_count):
        mock_count.return_value = 5
        migrated = self.migrator.migrate_table_data("chunks", dry_run=True)
        self.assertEqual(migrated, 5)
