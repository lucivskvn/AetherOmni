from unittest import TestCase
from unittest.mock import patch

from django.test import override_settings

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
            query_cleaned="What is KORDA?",
            context_str="KORDA is a knowledge operations workspace.",
            user_memories_block="",
            selected_model="gemini-2.5-flash",
        )
        self.assertEqual(answer, "This is a verified RAG answer [Smith, 2026].")
        mock_generate.assert_called_once()

    def test_generate_deterministic_embedding(self):
        import math

        from extractor.rag import generate_deterministic_embedding

        vec1 = generate_deterministic_embedding("Surah Al-Fatiha verse 1")
        self.assertEqual(len(vec1), 768)
        norm1 = math.sqrt(sum(x * x for x in vec1))
        self.assertTrue(math.isclose(norm1, 1.0, rel_tol=1e-5))

        # Deterministic reproducibility
        vec2 = generate_deterministic_embedding("Surah Al-Fatiha verse 1")
        self.assertEqual(vec1, vec2)

        # Distinct text produces distinct vector
        vec3 = generate_deterministic_embedding("Surah Al-Baqarah verse 255")
        self.assertNotEqual(vec1, vec3)

        # Empty string handling
        empty_vec = generate_deterministic_embedding("")
        self.assertEqual(len(empty_vec), 768)
        self.assertEqual(sum(empty_vec), 0.0)

    def test_generate_deterministic_embedding_finite_floats(self):
        import math

        from extractor.rag import generate_deterministic_embedding

        vec = generate_deterministic_embedding("Legal contract clause analysis with vector embeddings.")
        self.assertEqual(len(vec), 768)
        for elem in vec:
            self.assertTrue(isinstance(elem, float))
            self.assertTrue(math.isfinite(elem))
            self.assertFalse(math.isnan(elem))
            self.assertFalse(math.isinf(elem))

    @patch("extractor.llm_gateway.execute_embed_content_with_fallback", side_effect=RuntimeError("API Quota Limit"))
    def test_fetch_missing_embeddings_fallback(self, mock_embed):
        from extractor.rag import _fetch_missing_embeddings

        missing_indices = [0, 1]
        missing_texts = ["First chunk", "Second chunk"]
        result = _fetch_missing_embeddings(missing_indices, missing_texts, "text-embedding-004")

        self.assertEqual(len(result), 2)
        self.assertEqual(len(result[0]), 768)
        self.assertEqual(len(result[1]), 768)
        mock_embed.assert_called_once()

    @override_settings(SURREALDB_OFFLINE=False)
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback", side_effect=RuntimeError("API Error"))
    def test_fill_missing_fallbacks_sentinel_online(self, mock_embed):
        from extractor.rag import _EMBEDDING_FAILED_SENTINEL, _fill_missing_fallbacks

        final_embeddings = [None]
        chunks_list = ["Failed chunk"]
        _fill_missing_fallbacks(final_embeddings, chunks_list, "text-embedding-004")
        self.assertEqual(final_embeddings[0], _EMBEDDING_FAILED_SENTINEL)
        self.assertIsNone(final_embeddings[0])

    @override_settings(SURREALDB_OFFLINE=False)
    @patch("extractor.rag._lookup_cached_embeddings", return_value=([None], [0], ["Test chunk"]))
    @patch("extractor.rag._fetch_missing_embeddings", return_value={})
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback", side_effect=RuntimeError("API Error"))
    def test_generate_surreal_embeddings_failed_sentinel(self, mock_embed, mock_fetch, mock_lookup):
        from extractor.rag import _EMBEDDING_FAILED_SENTINEL, generate_surreal_embeddings

        embeddings = generate_surreal_embeddings(["Test chunk"])
        self.assertEqual(len(embeddings), 1)
        self.assertEqual(embeddings[0], _EMBEDDING_FAILED_SENTINEL)
