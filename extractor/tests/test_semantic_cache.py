from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor.models import SourceDocument
from extractor.utils import query_semantic_knowledge_rag


class SemanticCacheTestCase(TestCase):
    """
    Verifies vector-based semantic query caching behavior using SurrealDB.
    """

    def setUp(self):
        # Create dummy document
        self.doc = SourceDocument.objects.create(
            original_filename="test_doc.txt",
            file_hash="dummy-hash-999",
            title="Islamic History and Heritage",
            status="COMPLETED",
        )

    @patch("extractor.rag.generate_llm_content_unified")
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback")
    @patch("extractor.surreal_db.kv_cache_get")
    @patch("extractor.surreal_db.kv_cache_set")
    @patch("extractor.surreal_db.search_rag_cache_hnsw")
    @patch("extractor.surreal_db.upsert_rag_cache")
    @patch("extractor.surreal_db.search_chunks_hnsw")
    def test_semantic_cache_hit_and_miss(
        self, mock_search_chunks, mock_upsert_rag, mock_search_rag, mock_kv_set, mock_kv_get, mock_execute, mock_unified
    ):
        # 1. Mock embeds
        mock_emb_val = MagicMock()
        mock_emb_val.values = [0.2] * 768
        mock_query_resp = MagicMock()
        mock_query_resp.embeddings = [mock_emb_val]
        mock_execute.return_value = mock_query_resp

        # Mock RAG chunks search response
        mock_search_chunks.return_value = [
            {
                "id": "chunks:123",
                "doc_uuid": str(self.doc.uuid),
                "content": "The Abbasid Caliphate was founded in 750 CE.",
                "language": "English",
                "chunk_index": 0,
            }
        ]

        # Mock unified generator output
        mock_unified_resp = MagicMock()
        mock_unified_resp.text = "Answer about Abbasid Caliphate."
        mock_unified.return_value = mock_unified_resp

        # Mock cache misses
        mock_kv_get.return_value = None
        mock_search_rag.return_value = []

        # 2. First query: cache miss, runs full RAG pipeline
        res1 = query_semantic_knowledge_rag("tell me about abbasids", top_k=1)

        self.assertEqual(res1["answer"], "Answer about Abbasid Caliphate.")
        self.assertEqual(mock_unified.call_count, 1)
        mock_upsert_rag.assert_called_once()
        mock_kv_set.assert_called_once()

        # 3. Second query: mock semantic cache hit
        mock_search_rag.return_value = [
            {
                "id": "rag_cache:abc",
                "answer_text": "Answer about Abbasid Caliphate.",
                "sources": [str(self.doc.uuid)],
                "score": 0.05,
            }
        ]

        res2 = query_semantic_knowledge_rag("explain abbasid history", top_k=1)
        self.assertEqual(res2["answer"], "Answer about Abbasid Caliphate.")
        # LLM should not be called again
        self.assertEqual(mock_unified.call_count, 1)

    @patch("extractor.surreal_db.kv_cache_delete_pattern")
    def test_cache_invalidation_signals(self, mock_kv_delete):
        # Create another document in COMPLETED status to trigger post_save invalidation
        with self.settings(SURREALDB_OFFLINE=False):
            doc2 = SourceDocument.objects.create(
                original_filename="completed_doc.txt",
                file_hash="dummy-hash-888",
                title="Title",
                status="COMPLETED",
            )

        # kv_cache_delete_pattern should be called on save/delete to clear RAG caches
        mock_kv_delete.assert_called_with("rag_search_cache:")

        # Reset mock and delete document
        mock_kv_delete.reset_mock()
        with self.settings(SURREALDB_OFFLINE=False):
            doc2.delete()
        mock_kv_delete.assert_called_with("rag_search_cache:")
