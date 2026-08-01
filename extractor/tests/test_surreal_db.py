from unittest.mock import AsyncMock, MagicMock, patch

from django.test import TestCase

from extractor import surreal_db


class SurrealDBClientTestCase(TestCase):
    def setUp(self):
        from django.conf import settings

        self.original_offline = getattr(settings, "SURREALDB_OFFLINE", False)
        settings.SURREALDB_OFFLINE = False

    def tearDown(self):
        from django.conf import settings

        settings.SURREALDB_OFFLINE = self.original_offline

    def _create_mock_db(self, return_value=None):
        mock_db = MagicMock()
        mock_db.__aenter__.return_value = mock_db
        mock_db.signin = AsyncMock()
        mock_db.use = AsyncMock()
        mock_db.query = AsyncMock(return_value=return_value or [])
        return mock_db

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_check_health_online(self, mock_surreal):
        mock_db = self._create_mock_db()
        mock_surreal.return_value = mock_db
        self.assertTrue(surreal_db.check_health())
        mock_surreal.assert_called_once()

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_check_health_offline(self, mock_surreal):
        mock_surreal.side_effect = Exception("Connection refused")
        self.assertFalse(surreal_db.check_health())
        mock_surreal.assert_called_once()

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_recreate_chunks(self, mock_surreal):
        mock_db = self._create_mock_db()
        mock_surreal.return_value = mock_db
        doc_uuid = "00000000-0000-0000-0000-000000000000"
        chunks = [{"chunk_index": 0, "content": "Text", "embedding": [0.1] * 768}]

        surreal_db.recreate_chunks(doc_uuid, chunks)
        self.assertEqual(mock_db.query.call_count, 2)
        mock_db.query.assert_any_call(
            "DELETE FROM chunks WHERE doc_uuid = $doc_uuid;", {"doc_uuid": "00000000-0000-0000-0000-000000000000"}
        )

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_search_chunks_hnsw(self, mock_surreal):
        query_res = [
            {
                "result": [
                    {
                        "doc_uuid": "00000000-0000-0000-0000-000000000000",
                        "content": "Found text content",
                        "chunk_index": 0,
                    }
                ]
            }
        ]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        results = surreal_db.search_chunks_hnsw([0.1] * 768, limit=1)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["content"], "Found text content")
        self.assertEqual(results[0]["chunk_index"], 0)
        self.assertEqual(results[0]["doc_uuid"], "00000000-0000-0000-0000-000000000000")
        mock_db.query.assert_called_once()

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_kv_cache_set_and_get(self, mock_surreal):
        query_res = [{"result": [{"val": {"answer": "cached response"}}]}]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        val = surreal_db.kv_cache_get("my-key")
        self.assertEqual(val, {"answer": "cached response"})
        mock_db.query.assert_called()

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_create_document_filters_invalid_keys(self, mock_surreal):
        query_res = [{"result": [{"doc_uuid": "123", "title": "Test"}]}]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        data = {
            "doc_uuid": "123",
            "title": "Test",
            "malicious_key": "DROP TABLE documents",
        }
        res = surreal_db.create_document(data)
        self.assertEqual(res, {"doc_uuid": "123", "title": "Test"})

        # Check that the sql does not contain malicious_key
        call_args = mock_db.query.call_args[0]
        sql = call_args[0]
        self.assertNotIn("malicious_key", sql)
        self.assertIn("doc_uuid", sql)
        self.assertIn("title", sql)

    @patch("extractor.surreal_db.AsyncSurreal")
    def test_update_document_filters_invalid_keys(self, mock_surreal):
        query_res = [{"result": [{"doc_uuid": "123", "title": "Test Updated"}]}]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        data = {
            "title": "Test Updated",
            "malicious_key": "DROP TABLE documents",
        }
        res = surreal_db.update_document("123", data)
        self.assertEqual(res, {"doc_uuid": "123", "title": "Test Updated"})

        # Check that the sql does not contain malicious_key
        call_args = mock_db.query.call_args[0]
        sql = call_args[0]
        self.assertNotIn("malicious_key", sql)
        self.assertIn("title", sql)
