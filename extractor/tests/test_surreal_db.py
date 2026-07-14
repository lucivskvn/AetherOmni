from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor import surreal_db


class SurrealDBClientTestCase(TestCase):
    """
    Verifies that the SurrealDB client wrapper builds correct queries,
    handles HTTP responses, and handles connection failures gracefully.
    """

    def setUp(self):
        from django.conf import settings

        self.original_offline = getattr(settings, "SURREALDB_OFFLINE", False)
        settings.SURREALDB_OFFLINE = False

    def tearDown(self):
        from django.conf import settings

        settings.SURREALDB_OFFLINE = self.original_offline

    @patch("extractor.surreal_db.Surreal")
    def test_check_health_online(self, mock_surreal):
        mock_db = MagicMock()
        mock_db.__aenter__.return_value = mock_db
        mock_surreal.return_value = mock_db
        from unittest.mock import AsyncMock

        mock_db.signin = AsyncMock()
        mock_db.use = AsyncMock()
        mock_db.query = AsyncMock(
            side_effect=mock_db.query.side_effect if hasattr(mock_db.query, "side_effect") else None,
            return_value=mock_db.query.return_value,
        )

        self.assertTrue(surreal_db.check_health())

    @patch("extractor.surreal_db.Surreal")
    def test_check_health_offline(self, mock_surreal):
        mock_surreal.side_effect = Exception("Connection refused")

        self.assertFalse(surreal_db.check_health())

    @patch("extractor.surreal_db.Surreal")
    def test_recreate_chunks(self, mock_surreal):
        mock_db = MagicMock()
        mock_db.__aenter__.return_value = mock_db
        mock_surreal.return_value = mock_db
        from unittest.mock import AsyncMock

        mock_db.signin = AsyncMock()
        mock_db.use = AsyncMock()
        mock_db.query = AsyncMock(
            side_effect=mock_db.query.side_effect if hasattr(mock_db.query, "side_effect") else None,
            return_value=mock_db.query.return_value,
        )

        doc_uuid = "00000000-0000-0000-0000-000000000000"
        chunks = [{"chunk_index": 0, "content": "Text", "embedding": [0.1] * 768}]

        surreal_db.recreate_chunks(doc_uuid, chunks)

        self.assertEqual(mock_db.query.call_count, 2)
        mock_db.query.assert_any_call(
            "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;", {"doc_uuid": "00000000-0000-0000-0000-000000000000"}
        )

    @patch("extractor.surreal_db.Surreal")
    def test_search_chunks_hnsw(self, mock_surreal):
        mock_db = MagicMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.query.return_value = [
            {
                "result": [
                    {
                        "doc_uuid": "00000000-0000-0000-0000-000000000000",
                        "content": "Found text content",
                        "chunk_index": 0,
                    }
                ]
            },
        ]
        mock_surreal.return_value = mock_db
        from unittest.mock import AsyncMock

        mock_db.signin = AsyncMock()
        mock_db.use = AsyncMock()
        mock_db.query = AsyncMock(
            side_effect=mock_db.query.side_effect if hasattr(mock_db.query, "side_effect") else None,
            return_value=mock_db.query.return_value,
        )

        results = surreal_db.search_chunks_hnsw([0.1] * 768, limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content"], "Found text content")
        self.assertEqual(results[0]["chunk_index"], 0)
        self.assertEqual(results[0]["doc_uuid"], "00000000-0000-0000-0000-000000000000")
        mock_db.query.assert_called_once()

    @patch("extractor.surreal_db.Surreal")
    def test_kv_cache_set_and_get(self, mock_surreal):
        mock_db = MagicMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.query.return_value = [{"result": [{"val": {"answer": "cached response"}}]}]
        mock_surreal.return_value = mock_db
        from unittest.mock import AsyncMock

        mock_db.signin = AsyncMock()
        mock_db.use = AsyncMock()
        mock_db.query = AsyncMock(
            side_effect=mock_db.query.side_effect if hasattr(mock_db.query, "side_effect") else None,
            return_value=mock_db.query.return_value,
        )

        val = surreal_db.kv_cache_get("my-key")
        self.assertEqual(val, {"answer": "cached response"})
        mock_db.query.assert_called()
        self.assertEqual(mock_db.query.call_count, 1)

        surreal_db.kv_cache_set("my-key2", {"val": "data"})
        mock_db.query.assert_called()
        self.assertEqual(mock_db.query.call_count, 2)
