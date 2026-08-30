from unittest.mock import patch

from django.test import TestCase

from extractor.rag import ensure_document_chunks_loaded


class RagPerformanceTestCase(TestCase):
    @patch("extractor.surreal_db._run")
    def test_ensure_chunks_loaded_single_uuid(self, mock_run):
        # 1. Single UUID (string) should query database once
        mock_run.return_value = [
            {"status": "OK", "result": [{"n": 5, "doc_uuid": "11111111-1111-1111-1111-111111111111"}]}
        ]
        doc_uuid = "11111111-1111-1111-1111-111111111111"

        ensure_document_chunks_loaded(doc_uuid)
        self.assertEqual(mock_run.call_count, 1)

    @patch("extractor.surreal_db._run")
    def test_ensure_chunks_loaded_optimized_bulk(self, mock_run):
        # 2. Multiple UUIDs should query database exactly once (solving N+1!)
        mock_run.return_value = [
            {
                "status": "OK",
                "result": [
                    {"n": 5, "doc_uuid": "11111111-1111-1111-1111-111111111111"},
                    {"n": 2, "doc_uuid": "22222222-2222-2222-2222-222222222222"},
                    {"n": 10, "doc_uuid": "33333333-3333-3333-3333-333333333333"},
                ],
            }
        ]

        doc_uuids = [
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
            "33333333-3333-3333-3333-333333333333",
        ]

        ensure_document_chunks_loaded(doc_uuids)

        # Verify that only a SINGLE database call was made
        self.assertEqual(mock_run.call_count, 1)

        # Verify the query has $doc_uuids passed
        args, kwargs = mock_run.call_args
        params = args[1] if len(args) > 1 else kwargs.get("params", {})
        self.assertIn("doc_uuids", params)
        self.assertEqual(params["doc_uuids"], doc_uuids)

    def test_sync_postgres_memories_to_surreal_queries(self):
        from unittest.mock import MagicMock

        from django.contrib.auth import get_user_model

        from extractor.models import UserMemory
        from extractor.rag import _sync_postgres_memories_to_surreal

        User = get_user_model()
        user = User.objects.create_user(username="testsyncuser", password="password")
        UserMemory.objects.create(user=user, memory_text="mem1", embedding=[0.1] * 768)
        UserMemory.objects.create(user=user, memory_text="mem2", embedding=[0.2] * 768)

        mock_surreal_db = MagicMock()

        with self.assertNumQueries(1):
            _sync_postgres_memories_to_surreal(user, mock_surreal_db, UserMemory)

        self.assertEqual(mock_surreal_db.add_user_memory.call_count, 2)
