from datetime import UTC, datetime
from decimal import Decimal
from threading import get_ident
from unittest.mock import AsyncMock, MagicMock, call, patch

from django.test import TestCase, override_settings

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

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_check_health_running_event_loop_dispatches_to_thread(self, mock_surreal):
        mock_db = self._create_mock_db()
        caller_thread = get_ident()
        constructor_threads = []

        def construct_db(*args, **kwargs):
            constructor_threads.append(get_ident())
            return mock_db

        mock_surreal.side_effect = construct_db

        import asyncio

        async def run_test():
            result = surreal_db.check_health()
            await asyncio.sleep(0)
            return result

        result = asyncio.run(run_test())
        self.assertTrue(result)
        mock_surreal.assert_called()
        self.assertTrue(constructor_threads)
        self.assertTrue(all(thread_id != caller_thread for thread_id in constructor_threads))

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_run_running_event_loop_dispatches_to_thread(self, mock_surreal):
        query_res = [{"result": [{"status": "OK"}]}]
        mock_db = self._create_mock_db(query_res)
        caller_thread = get_ident()
        constructor_threads = []

        def construct_db(*args, **kwargs):
            constructor_threads.append(get_ident())
            return mock_db

        mock_surreal.side_effect = construct_db

        import asyncio

        async def run_test():
            result = surreal_db._run("SELECT * FROM documents;")
            await asyncio.sleep(0)
            return result

        result = surreal_db._first_result(asyncio.run(run_test()))
        self.assertEqual(result, [{"status": "OK"}])
        mock_db.query.assert_called_once()
        self.assertTrue(constructor_threads)
        self.assertTrue(all(thread_id != caller_thread for thread_id in constructor_threads))

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_check_health_online(self, mock_surreal):
        mock_db = self._create_mock_db()
        mock_surreal.return_value = mock_db
        self.assertTrue(surreal_db.check_health())
        mock_surreal.assert_called_once()

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_check_health_offline(self, mock_surreal):
        mock_surreal.side_effect = Exception("Connection refused")
        self.assertFalse(surreal_db.check_health())
        mock_surreal.assert_called_once()

    @override_settings(SURREALDB_OFFLINE=True)
    @patch("extractor.models.MonthlySpendLog.add_cost", return_value=True)
    def test_flush_document_cost_accepts_surreal_datetime(self, mock_add_cost):
        created_at = datetime(2026, 8, 12, tzinfo=UTC)

        self.assertTrue(
            surreal_db._flush_document_cost(
                {"created_at": created_at, "cost_usd": 1.25, "input_tokens": 10, "output_tokens": 20}
            )
        )
        self.assertTrue(
            surreal_db._flush_document_cost(
                {"created_at": "2026-08-12T00:00:00Z", "cost_usd": 1.25, "input_tokens": 10, "output_tokens": 20}
            )
        )
        mock_add_cost.assert_has_calls(
            [
                call(date=created_at, cost=Decimal("1.25"), in_tok=10, out_tok=20),
                call(date=datetime(2026, 8, 12, tzinfo=UTC), cost=Decimal("1.25"), in_tok=10, out_tok=20),
            ]
        )

    @override_settings(SURREALDB_OFFLINE=True)
    @patch("extractor.models.MonthlySpendLog.add_cost", return_value=False)
    def test_flush_document_cost_reports_ledger_persistence_failure(self, mock_add_cost):
        self.assertFalse(surreal_db._flush_document_cost({"created_at": "2026-08-12T00:00:00Z", "cost_usd": 1.25}))
        mock_add_cost.assert_called_once()

    @override_settings(SURREALDB_OFFLINE=True)
    @patch("extractor.models.MonthlySpendLog.add_cost")
    def test_flush_document_cost_rejects_unsupported_timestamp(self, mock_add_cost):
        self.assertFalse(surreal_db._flush_document_cost({"created_at": 42, "cost_usd": 1.25}))
        mock_add_cost.assert_not_called()

    @patch("extractor.surreal_db._run")
    @patch("extractor.surreal_db._flush_document_cost", return_value=False)
    @patch("extractor.surreal_db.get_document", return_value={"cost_usd": 1.25, "created_at": "not-a-date"})
    def test_delete_document_preserves_paid_document_when_spend_cannot_flush(self, _mock_doc, _mock_flush, mock_run):
        with self.assertRaisesRegex(RuntimeError, "spend ledger"):
            surreal_db.delete_document("paid-document")
        mock_run.assert_not_called()

    @override_settings(DEBUG=True)
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

    @override_settings(DEBUG=True)
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

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_kv_cache_set_and_get(self, mock_surreal):
        query_res = [{"result": [{"val": {"answer": "cached response"}}]}]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        val = surreal_db.kv_cache_get("my-key")
        self.assertEqual(val, {"answer": "cached response"})
        mock_db.query.assert_called()

    @override_settings(DEBUG=True)
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

        # Check that the payload parameters do not contain malicious_key
        call_args = mock_db.query.call_args[0]
        sql = call_args[0]
        params = call_args[1] if len(call_args) > 1 else mock_db.query.call_args[1].get("params", {})
        payload = params.get("payload", params)
        self.assertNotIn("malicious_key", str(sql))
        self.assertNotIn("malicious_key", payload)
        self.assertIn("doc_uuid", payload)
        self.assertIn("title", payload)

    @override_settings(DEBUG=True)
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

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_context_cache_flow(self, mock_surreal):
        mock_db = self._create_mock_db(
            [{"result": [{"context_hash": "abc", "context_text": "sample text", "hit_count": 0}]}]
        )
        mock_surreal.return_value = mock_db

        surreal_db.context_cache_set("abc", "sample text", token_count=10)
        res = surreal_db.context_cache_get("abc")
        self.assertIsNotNone(res)
        self.assertEqual(res.get("context_hash"), "abc")

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_rate_limiting_flow(self, mock_surreal):
        mock_db = self._create_mock_db([[]])
        mock_surreal.return_value = mock_db

        # First request allowed (initial entry created)
        allowed = surreal_db.check_rate_limit_atomic("user:123", max_requests=5)
        self.assertTrue(allowed)

    def test_update_document_offline_nonexistent(self):
        res = surreal_db._update_document_offline("00000000-0000-0000-0000-000000000000", {"title": "New Title"})
        self.assertEqual(res, {})

    def test_delete_offline_document_nonexistent(self):
        # Should not raise exception
        surreal_db._delete_offline_document("00000000-0000-0000-0000-000000000000")
        surreal_db._delete_offline_document("not-a-valid-int-or-uuid")

    @override_settings(SURREALDB_OFFLINE=True)
    def test_find_chunk_embeddings_batch_offline(self):
        res = surreal_db.find_chunk_embeddings_batch(["nonexistent-uuid-1", "nonexistent-uuid-2"])
        self.assertEqual(res, {})

    @override_settings(SURREALDB_OFFLINE=True)
    def test_delete_offline_document_purges_chunks(self):
        from extractor.models import SourceDocument

        doc = SourceDocument.objects.create(
            original_filename="offline_test.pdf",
            file_hash="offline_hash_123",
            title="Offline Test Doc",
        )
        doc_uuid = str(doc.uuid)
        chunks = [{"chunk_index": 0, "content": "Sample offline text", "embedding": [0.0] * 768}]
        surreal_db.recreate_chunks(doc_uuid, chunks)
        self.assertEqual(surreal_db.count_document_chunks(doc_uuid), 1)

        surreal_db.delete_document(doc_uuid)
        self.assertEqual(surreal_db.count_document_chunks(doc_uuid), 0)
        self.assertFalse(SourceDocument.objects.filter(uuid=doc_uuid).exists())

    @override_settings(DEBUG=True)
    @patch("extractor.surreal_db.AsyncSurreal")
    def test_create_document_converts_datetime_strings_to_datetime_objects(self, mock_surreal):
        query_res = [{"result": [{"doc_uuid": "123", "title": "Test"}]}]
        mock_db = self._create_mock_db(query_res)
        mock_surreal.return_value = mock_db

        data = {
            "doc_uuid": "123",
            "title": "Test",
            "created_at": "2026-08-16T07:33:18Z",
            "updated_at": "2026-08-16T07:33:20+00:00",
            "expires_at": datetime(2026, 8, 17, 12, 0, 0, tzinfo=UTC),
        }
        surreal_db.create_document(data)

        call_args = mock_db.query.call_args[0]
        params = call_args[1] if len(call_args) > 1 else mock_db.query.call_args[1].get("params", {})
        payload = params.get("payload", params)

        self.assertIsInstance(payload["created_at"], datetime)
        self.assertEqual(payload["created_at"].tzinfo, UTC)
        self.assertIsInstance(payload["updated_at"], datetime)
        self.assertEqual(payload["updated_at"].tzinfo, UTC)
        self.assertIsInstance(payload["expires_at"], datetime)

    @override_settings(SURREALDB_OFFLINE=True)
    def test_get_documents_mixed_ids_offline(self):
        from extractor.models import SourceDocument

        doc1 = SourceDocument.objects.create(
            original_filename="doc1.pdf",
            file_hash="hash_doc_1",
            title="Doc 1",
        )
        doc2 = SourceDocument.objects.create(
            original_filename="doc2.pdf",
            file_hash="hash_doc_2",
            title="Doc 2",
        )
        # Query using a mix of integer ID (str or int) and UUID string
        res = surreal_db.get_documents([str(doc1.id), str(doc2.uuid)])
        self.assertEqual(len(res), 2)
        resolved_titles = {d["title"] for d in res}
        self.assertEqual(resolved_titles, {"Doc 1", "Doc 2"})
