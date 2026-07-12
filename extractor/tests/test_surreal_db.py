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

    @patch("extractor.surreal_db.get_surreal_client")
    def test_check_health_online(self, mock_get_client):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_client.get.return_value = mock_resp
        mock_get_client.return_value = mock_client

        self.assertTrue(surreal_db.check_health())
        mock_client.get.assert_called_once()
        mock_client.get.assert_called_with("/health")
        self.assertEqual(mock_resp.status_code, 200)

    @patch("extractor.surreal_db.get_surreal_client")
    def test_check_health_offline(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get.side_effect = Exception("Connection refused")
        mock_get_client.return_value = mock_client

        self.assertFalse(surreal_db.check_health())
        mock_client.get.assert_called_once_with("/health")

    @patch("extractor.surreal_db.get_surreal_client")
    def test_recreate_chunks(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        doc_uuid = "00000000-0000-0000-0000-000000000000"
        chunks = [{"chunk_index": 0, "content": "Text", "embedding": [0.1] * 768}]

        surreal_db.recreate_chunks(doc_uuid, chunks)

        # Should delete existing chunks first, then insert new ones
        self.assertEqual(mock_client.post.call_count, 2)
        mock_client.post.assert_any_call(
            "/sql",
            content=b'LET $doc_uuid = "00000000-0000-0000-0000-000000000000";\nDELETE FROM chunks WHERE doc_uuid = $doc_uuid;',
            headers={
                "Accept": "application/json",
                "NS": "omnirag",
                "DB": "extractor",
                "surreal-ns": "omnirag",
                "surreal-db": "extractor",
                "Content-Type": "text/plain",
            },
        )

    @patch("extractor.surreal_db.get_surreal_client")
    def test_search_chunks_hnsw(self, mock_get_client):
        mock_client = MagicMock()
        mock_resp = MagicMock()
        mock_resp.json.return_value = [
            {"result": None},
            {"result": None},
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
        mock_client.post.return_value = mock_resp
        mock_get_client.return_value = mock_client

        results = surreal_db.search_chunks_hnsw([0.1] * 768, limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content"], "Found text content")
        self.assertEqual(results[0]["chunk_index"], 0)
        self.assertEqual(results[0]["doc_uuid"], "00000000-0000-0000-0000-000000000000")
        mock_client.post.assert_called_once()

    @patch("extractor.surreal_db.get_surreal_client")
    def test_kv_cache_set_and_get(self, mock_get_client):
        mock_client = MagicMock()
        mock_get_client.return_value = mock_client

        # Mock GET response
        mock_resp_get = MagicMock()
        mock_resp_get.json.return_value = [{"result": None}, {"result": [{"val": {"answer": "cached response"}}]}]
        mock_client.post.return_value = mock_resp_get

        val = surreal_db.kv_cache_get("my-key")
        self.assertEqual(val, {"answer": "cached response"})
        mock_client.post.assert_called()
        self.assertEqual(mock_client.post.call_count, 1)

        # Test set cache
        surreal_db.kv_cache_set("my-key2", {"val": "data"})
        # Should post query to SurrealDB
        mock_client.post.assert_called()
        self.assertEqual(mock_client.post.call_count, 2)
