from unittest import TestCase
from unittest.mock import patch

from extractor.rag import reciprocal_rank_fusion
from extractor.surreal_db import search_chunks_bm25


class HybridRAGTestCase(TestCase):
    def test_reciprocal_rank_fusion(self):
        dense_results = [
            {"id": "doc1_chunk0", "content": "Dense chunk 1", "score": 0.1},
            {"id": "doc1_chunk1", "content": "Dense chunk 2", "score": 0.2},
        ]
        sparse_results = [
            {"id": "doc1_chunk1", "content": "Dense chunk 2"},
            {"id": "doc2_chunk0", "content": "Sparse chunk 1"},
        ]

        fused = reciprocal_rank_fusion(dense_results, sparse_results, k=60, top_k=2)
        self.assertEqual(len(fused), 2)
        # doc1_chunk1 appears in both lists so its RRF score is highest
        self.assertEqual(fused[0]["id"], "doc1_chunk1")

    def test_search_chunks_bm25_offline(self):
        results = search_chunks_bm25("test", limit=5)
        self.assertIsInstance(results, list)

    @patch("extractor.rag.generate_llm_content_unified")
    def test_generate_rag_answer(self, mock_generate):
        from extractor.rag import _generate_rag_answer

        mock_generate.return_value = "This is a verified RAG answer [Smith, 2026]."
        answer = _generate_rag_answer(
            query_cleaned="What is AetherOmni?",
            context_str="AetherOmni is an Enterprise Document Intelligence platform.",
            user_memories_block="",
            selected_model="gemini-2.5-flash",
        )
        self.assertEqual(answer, "This is a verified RAG answer [Smith, 2026].")
        mock_generate.assert_called_once()
