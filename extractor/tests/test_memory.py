from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor.models import SourceDocument, SystemSettings
from extractor.rag import is_preference_signal
from extractor.utils import query_semantic_knowledge_rag


class MultiModelMemoryCachingTestCase(TestCase):
    """Verifies hybrid memory intent detection, fallback cascades, and SurrealDB RAG response caching."""

    def setUp(self):
        self.settings_obj = SystemSettings.get_settings()
        self.settings_obj.selected_model = "auto"
        self.settings_obj.save()

    def test_is_preference_signal(self):
        # High intent preference signals
        self.assertTrue(is_preference_signal("remember that I prefer extremely short answers"))
        self.assertTrue(is_preference_signal("always write responses in Arabic"))
        self.assertTrue(is_preference_signal("I prefer classical sources"))

        # Simple Q&As (should be gated / skipped)
        self.assertFalse(is_preference_signal("what is the capital of Saudi Arabia?"))
        self.assertFalse(is_preference_signal("when was this book written?"))

    @patch("extractor.llm_gateway._call_direct_gemini")
    def test_progressive_deprecation_fallback_chain(self, mock_direct_gemini):
        # We simulate a "Not Found" error for the first model attempt, triggering a fallback cascade
        mock_response = MagicMock()
        mock_response.text = "Grounded answer from stable fallback."
        mock_response.input_tokens = 100
        mock_response.output_tokens = 50
        mock_response.cost_usd = Decimal("0.0001")
        mock_response.model_used = "gemini-3.1-flash-lite"

        # First call throws, second call succeeds
        mock_direct_gemini.side_effect = [
            Exception("API Error: Model 'gemini-9.9-flash' not found. Please use stable alternatives."),
            mock_response,
        ]

        from extractor.utils import generate_llm_content_unified

        with self.assertLogs("extractor.llm_gateway", level="WARNING") as log_capture:
            result = generate_llm_content_unified(prompt="Hello", model_name="gemini-9.9-flash")

        self.assertEqual(result.text, "Grounded answer from stable fallback.")
        self.assertEqual(result.model_used, "gemini-3.1-flash-lite")
        # Assert direct gemini was called twice (initial failed attempt + fallback successful attempt)
        self.assertEqual(mock_direct_gemini.call_count, 2)
        self.assertTrue(
            any("Model 'gemini-3.5-flash' failed or rate-limited" in message for message in log_capture.output)
        )

    @patch("extractor.llm_gateway._call_openrouter")
    @patch("extractor.llm_gateway._call_direct_gemini")
    @patch("os.getenv")
    def test_cross_provider_429_failover(self, mock_getenv, mock_direct_gemini, mock_openrouter):
        # We simulate that the OPENROUTER_API_KEY is configured
        mock_getenv.side_effect = lambda key, default=None: (
            "mock-openrouter-key" if key == "OPENROUTER_API_KEY" else default
        )

        # We simulate a "429 Resource Exhausted" rate-limit error for all direct Gemini attempts
        mock_direct_gemini.side_effect = Exception("API Error: 429 Resource Exhausted. Daily quota exceeded.")

        # We mock a successful OpenRouter reply from Llama 3 8B Free
        mock_response = MagicMock()
        mock_response.text = "Answer from OpenRouter free-tier backup."
        mock_response.model_used = "meta-llama/llama-3-8b-instruct:free"
        mock_openrouter.return_value = mock_response

        from extractor.utils import generate_llm_content_unified

        with self.assertLogs("extractor.llm_gateway", level="WARNING"):
            result = generate_llm_content_unified(prompt="Hello", model_name="gemini-3.1-flash-lite")

        # Ensure it fell back successfully to OpenRouter llama free-tier
        self.assertEqual(result.text, "Answer from OpenRouter free-tier backup.")
        self.assertEqual(result.model_used, "meta-llama/llama-3-8b-instruct:free")
        # Direct Gemini should have been tried for fallback candidates
        self.assertTrue(mock_direct_gemini.call_count > 1)
        mock_openrouter.assert_called_once()

    @patch("extractor.rag.generate_llm_content_unified")
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback")
    @patch("extractor.surreal_db.kv_cache_get")
    @patch("extractor.surreal_db.kv_cache_set")
    @patch("extractor.surreal_db.search_rag_cache_hnsw")
    @patch("extractor.surreal_db.search_chunks_hnsw")
    def test_surreal_rag_response_caching(
        self, mock_search_chunks, mock_search_rag, mock_kv_set, mock_kv_get, mock_execute, mock_unified
    ):
        doc = SourceDocument.objects.create(
            original_filename="sample_rag.txt", file_hash="mock-rag-hash", title="Islamic History", status="COMPLETED"
        )

        # Mock embeddings output
        mock_emb_val = MagicMock()
        mock_emb_val.values = [0.1] * 768
        mock_query_resp = MagicMock()
        mock_query_resp.embeddings = [mock_emb_val]
        mock_execute.return_value = mock_query_resp

        # Mock RAG chunks search response
        mock_search_chunks.return_value = [
            {
                "id": "chunks:123",
                "doc_uuid": str(doc.uuid),
                "content": "Caliphate history content",
                "language": "English",
                "chunk_index": 0,
            }
        ]

        # Mock unified generator output
        mock_unified_resp = MagicMock()
        mock_unified_resp.text = "Calculated history answer."
        mock_unified.return_value = mock_unified_resp

        # Mock cache miss for first search, cache hit for second
        mock_kv_get.side_effect = [None, {"answer": "Calculated history answer.", "sources": []}]
        mock_search_rag.return_value = []

        # First search: executes model
        with self.settings(GEMINI_API_KEY="mock-api-key"):
            res1 = query_semantic_knowledge_rag("history query", top_k=1)

        self.assertEqual(res1["answer"], "Calculated history answer.")
        self.assertEqual(mock_unified.call_count, 1)

        # Second search: hits KV cache
        with self.settings(GEMINI_API_KEY="mock-api-key"):
            res2 = query_semantic_knowledge_rag("history query", top_k=1)

        self.assertEqual(res2["answer"], "Calculated history answer.")
        self.assertEqual(mock_unified.call_count, 1)
