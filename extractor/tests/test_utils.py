import os
import tempfile
from unittest.mock import MagicMock, patch

from django.core.cache import cache
from django.test import RequestFactory, TestCase

from extractor.models import SourceDocument
from extractor.utils import (
    calculate_file_sha256,
    calculate_gemini_cost,
    chunk_document_semantically,
    format_localized_cost,
    get_locale_currency_details,
    process_csv_local,
    process_excel_local,
    process_json_local,
    process_pdf_local,
    process_txt_local,
    validate_url_scheme,
)


class LocalParsersTestCase(TestCase):
    """Verifies that offline CSV, Excel, JSON, TXT, and digital PDF local parsers function with $0 API cost."""

    def test_csv_parser_success(self):
        # Create a temp CSV file
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".csv", encoding="utf-8") as f:
            f.write("Name,Language,Cost\nBook 1,Arabic,0.02\nBook 2,English,0.05")
            temp_path = f.name

        try:
            markdown_table = process_csv_local(temp_path)
            self.assertIn("| Name | Language | Cost |", markdown_table)
            self.assertIn("| Book 1 | Arabic | 0.02 |", markdown_table)
            self.assertIn("| Book 2 | English | 0.05 |", markdown_table)
        finally:
            os.unlink(temp_path)

    def test_txt_parser_success(self):
        # Create a temp TXT file
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt", encoding="utf-8") as f:
            f.write("Islamic Knowledge Extract\nPage 1 Content")
            temp_path = f.name

        try:
            content = process_txt_local(temp_path)
            self.assertEqual(content, "Islamic Knowledge Extract\nPage 1 Content")
        finally:
            os.unlink(temp_path)

    def test_txt_parser_quran_arabic_and_translations(self):
        # Test UTF-8 Quranic verses with Harakat and multi-language translations
        quran_text = (
            "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ (1)\n"
            "ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَـٰلَمِينَ (2)\n"
            "Translation (EN): In the Name of Allah—the Most Compassionate, Most Merciful. (1)\n"
            "Terjemahan (ID): Dengan nama Allah Yang Maha Pengasih, Maha Penyayang. (1)"
        )
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".txt", encoding="utf-8") as f:
            f.write(quran_text)
            temp_path = f.name

        try:
            content = process_txt_local(temp_path)
            self.assertIn("بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ", content)
            self.assertIn("In the Name of Allah", content)
            self.assertIn("Dengan nama Allah", content)
        finally:
            os.unlink(temp_path)

    def test_excel_parser_empty(self):
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".xlsx") as f:
            f.write("invalid binary data")
            temp_path = f.name

        try:
            result = process_excel_local(temp_path)
            self.assertIn("Empty or Unreadable", result)
        finally:
            os.unlink(temp_path)

    def test_pdf_parser_uninstalled_or_empty(self):
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".pdf") as f:
            f.write("plain binary")
            temp_path = f.name

        try:
            text, _pages = process_pdf_local(temp_path)
            self.assertEqual(text, "")
        finally:
            os.unlink(temp_path)

    def test_json_parser_dataset_and_dict(self):
        # 1. Test tabular JSON list (e.g., Quran verse dataset)
        verse_data = [
            {"surah": "1", "ayah": "1", "text_ar": "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ", "text_en": "In the Name of Allah"},
            {"surah": "1", "ayah": "2", "text_ar": "ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَـٰلَمِينَ", "text_en": "Praise be to Allah"},
        ]
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json", encoding="utf-8") as f:
            import json

            json.dump(verse_data, f)
            temp_path = f.name

        try:
            result = process_json_local(temp_path)
            self.assertIn("| surah | ayah | text_ar | text_en |", result)
            self.assertIn("بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ", result)
        finally:
            os.unlink(temp_path)

        # 2. Test structured JSON dict
        hadith_dict = {
            "book_title": "Sahih Hadith Collection",
            "narrator": "Abu Hurairah",
            "hadith_text": "Actions are judged by intentions.",
        }
        with tempfile.NamedTemporaryFile(mode="w+", delete=False, suffix=".json", encoding="utf-8") as f:
            json.dump(hadith_dict, f)
            temp_path = f.name

        try:
            result_dict = process_json_local(temp_path)
            self.assertIn("## Book Title", result_dict)
            self.assertIn("Sahih Hadith Collection", result_dict)
            self.assertIn("Actions are judged by intentions.", result_dict)
        finally:
            os.unlink(temp_path)

    def test_semantic_chunking_islamic_structural_boundaries(self):
        text = (
            "## Page 1\n\n"
            "### Surah Al-Fatiha\n\n"
            "بِسْمِ ٱللَّهِ ٱلرَّحْمَـٰنِ ٱلرَّحِيمِ (1)\n"
            "ٱلْحَمْدُ لِلَّهِ رَبِّ ٱلْعَـٰلَمِينَ (2)\n\n"
            "### Surah Al-Baqarah\n\n"
            "الم (1)\n"
            "ذَٰلِكَ ٱلْكِتَـٰبُ لَا رَيْبَ ۛ فِيهِ ۛ هُدًۭى لِّلْمُتَّقِينَ (2)"
        )
        chunks = chunk_document_semantically(text, max_chunk_size=150)
        self.assertGreaterEqual(len(chunks), 2)
        # Verify Surah headers remain grouped with their verses
        self.assertTrue(any("Surah Al-Fatiha" in c for c in chunks))
        self.assertTrue(any("Surah Al-Baqarah" in c for c in chunks))


class ContentAddressingTestCase(TestCase):
    """Verifies SHA-256 hash calculation and de-duplication rules."""

    def test_sha256_calculator(self):
        # Create a temporary file
        content_bytes = b"Sample booklet bytes for hashing"
        with tempfile.NamedTemporaryFile(mode="wb+", delete=False) as f:
            f.write(content_bytes)
            temp_path = f.name

        try:
            file_hash = calculate_file_sha256(temp_path)
            # Match against expected sha256 of 'Sample booklet bytes for hashing'
            expected_hash = "9f613fcfa103ec3806438aceacaef2099ddc71ec5cf9c4b2c5e8e7f4b2622f5b"
            self.assertEqual(file_hash, expected_hash)
        finally:
            os.unlink(temp_path)


class CurrencyExchangeTestCase(TestCase):
    """Verifies that USD cost results are dynamically localized based on client context."""

    def setUp(self):
        self.factory = RequestFactory()
        cache.clear()

    def test_browser_locale_detection_idr(self):
        request = self.factory.get("/", HTTP_ACCEPT_LANGUAGE="id-ID,id;q=0.9,en-US;q=0.8")
        # Inject mock rate cache
        cache.set("usd_exchange_rates", {"IDR": 16300.0})

        details = get_locale_currency_details(request)
        self.assertEqual(details["currency_code"], "IDR")
        self.assertEqual(details["symbol"], "Rp ")
        self.assertEqual(details["rate"], 16300.0)

        # Test localized cost format
        formatted = format_localized_cost(0.027, details)
        self.assertIn("Rp 440", formatted)
        self.assertIn("~$0.0270", formatted)

    def test_browser_locale_detection_sar(self):
        request = self.factory.get("/", HTTP_ACCEPT_LANGUAGE="ar-SA,ar;q=0.9,en;q=0.8")
        cache.set("usd_exchange_rates", {"SAR": 3.75})

        details = get_locale_currency_details(request)
        self.assertEqual(details["currency_code"], "SAR")
        self.assertEqual(details["symbol"], "SR ")
        self.assertEqual(details["rate"], 3.75)


class CostCalculatorTestCase(TestCase):
    """Verifies billing calculation math for Gemini APIs."""

    def test_cost_calculation_flash_standard(self):
        # 10k input, 5k output, within 128k context
        cost = calculate_gemini_cost("gemini-3.5-flash", 10000, 5000)
        # Expected: (10000/1M * 1.50) + (5000/1M * 9.00) = 0.0150 + 0.0450 = 0.0600 USD
        self.assertAlmostEqual(float(cost), 0.0600)

    def test_cost_calculation_pro_standard(self):
        # 10k input, 5k output, within 128k context
        cost = calculate_gemini_cost("gemini-2.5-pro", 10000, 5000)
        # Expected: (10000/1M * 1.25) + (5000/1M * 10.00) = 0.0125 + 0.0500 = 0.0625 USD
        self.assertAlmostEqual(float(cost), 0.0625)


class SemanticChunkerTestCase(TestCase):
    """Verifies that markdown files split nicely on clean double newline boundaries."""

    def test_chunker_basic(self):
        text = "Paragraph 1 is here.\n\nParagraph 2 is slightly longer but splits on newline boundaries.\n\nParagraph 3 is also here."
        chunks = chunk_document_semantically(text, max_chunk_size=100)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(
            chunks[0], "Paragraph 1 is here.\n\nParagraph 2 is slightly longer but splits on newline boundaries."
        )
        self.assertEqual(chunks[1], "Paragraph 3 is also here.")


class SurrealDBSearchTestCase(TestCase):
    """Verifies that SurrealDB vector search integration ranks and returns sources perfectly."""

    @patch("extractor.rag.generate_llm_content_unified")
    @patch("extractor.llm_gateway.execute_embed_content_with_fallback")
    @patch("extractor.surreal_db.search_chunks_hnsw")
    def test_surreal_hnsw_search(self, mock_search_chunks, mock_execute, mock_generate):
        # Create a mock source document
        doc = SourceDocument.objects.create(
            original_filename="test_doc.pdf",
            file_hash="mock-hash-123",
            title="Islamic Knowledge",
            author="Scholar A",
            language="Arabic",
            status="COMPLETED",
        )

        query_emb = [1.0, 0.0] * 384

        # Mock the SurrealDB search response
        mock_search_chunks.return_value = [
            {"doc_uuid": str(doc.uuid), "content": "Content of chunk 1", "chunk_index": 1},
            {"doc_uuid": str(doc.uuid), "content": "Content of chunk 2", "chunk_index": 2},
            {"doc_uuid": str(doc.uuid), "content": "Content of chunk 3", "chunk_index": 3},
        ]

        # Mock the Gemini API embedding call response
        mock_emb_val = MagicMock()
        mock_emb_val.values = query_emb
        mock_query_resp = MagicMock()
        mock_query_resp.embeddings = [mock_emb_val]
        mock_execute.return_value = mock_query_resp

        # Mock the Unified LLM gateway call response
        mock_unified_resp = MagicMock()
        mock_unified_resp.text = "Mock RAG Answer based on grounded sources."
        mock_generate.return_value = mock_unified_resp

        # Run query with temporary GEMINI_API_KEY setting set
        with self.settings(GEMINI_API_KEY="mock-api-key"):
            from extractor.utils import query_semantic_knowledge_rag

            results = query_semantic_knowledge_rag(
                query="test query about islamic knowledge", document_ids=[doc.id], top_k=5
            )

        self.assertIsInstance(results, dict)
        self.assertIn("answer", results)
        self.assertEqual(results["answer"], "Mock RAG Answer based on grounded sources.")
        self.assertIn("sources", results)

        # Verify order of sources: Chunk 1, Chunk 2, Chunk 3
        sources = results["sources"]
        self.assertEqual(len(sources), 3)
        self.assertEqual(sources[0]["chunk_index"], 1)
        self.assertEqual(sources[1]["chunk_index"], 2)
        self.assertEqual(sources[2]["chunk_index"], 3)


class TemplateFiltersTestCase(TestCase):
    """Verifies custom template filters."""

    def test_dict_pct_success(self):
        from extractor.templatetags.extractor_filters import dict_pct

        self.assertEqual(dict_pct(30, 100), 30)
        self.assertEqual(dict_pct(0, 100), 0)
        self.assertEqual(dict_pct(50, 0), 100)
        self.assertEqual(dict_pct(None, None), 100)
        self.assertEqual(dict_pct("invalid", "total"), 0)

    def test_format_compact_tokens(self):
        from extractor.templatetags.extractor_filters import format_compact_tokens

        self.assertEqual(format_compact_tokens(1500000), "1.5M")
        self.assertEqual(format_compact_tokens(45200), "45.2K")
        self.assertEqual(format_compact_tokens(350), "350")
        self.assertEqual(format_compact_tokens(None), "0")
        self.assertEqual(format_compact_tokens("invalid"), "0")

    def test_normalize_language(self):
        from extractor.templatetags.extractor_filters import normalize_language

        self.assertEqual(normalize_language("ar"), "Arabic")
        self.assertEqual(normalize_language("id"), "Indonesian")
        self.assertEqual(normalize_language("bahasa"), "Indonesian")
        self.assertEqual(normalize_language("english"), "English")
        self.assertEqual(normalize_language("Unknown"), "Unknown")
        self.assertEqual(normalize_language("xyz"), "Xyz")
        self.assertEqual(normalize_language(""), "Unknown")
        self.assertEqual(normalize_language(None), "Unknown")

    @patch("time.time")
    @patch.dict(os.environ, {}, clear=True)
    def test_cache_bust_fallback_time(self, mock_time):
        from extractor.templatetags import extractor_filters

        extractor_filters._CACHE_BUST_VAL = None
        mock_time.return_value = 123456789

        # Test empty URLs
        self.assertEqual(extractor_filters.cache_bust(""), "")
        self.assertEqual(extractor_filters.cache_bust(None), "")

        # Test path without query param
        res = extractor_filters.cache_bust("/static/css/main.css")
        self.assertEqual(res, "/static/css/main.css?v=123456789")

        # Test path with existing query param
        res_with_q = extractor_filters.cache_bust("/static/css/main.css?theme=dark")
        self.assertEqual(res_with_q, "/static/css/main.css?theme=dark&v=123456789")

        # Clean up
        extractor_filters._CACHE_BUST_VAL = None

    @patch.dict(os.environ, {"RELEASE_VERSION": "v1.2.3_test"})
    def test_cache_bust_release_version(self):
        from extractor.templatetags import extractor_filters

        extractor_filters._CACHE_BUST_VAL = None

        # Test path without query param
        res = extractor_filters.cache_bust("/static/js/app.js")
        self.assertEqual(res, "/static/js/app.js?v=v1.2.3_test")

        # Test path with existing query param
        res_with_q = extractor_filters.cache_bust("/static/js/app.js?debug=true")
        self.assertEqual(res_with_q, "/static/js/app.js?debug=true&v=v1.2.3_test")

        # Clean up
        extractor_filters._CACHE_BUST_VAL = None


# AdminTestCase removed since DocumentChunk model has been retired.


class LocalParsersEncodingFallbackTestCase(TestCase):
    """Verifies that local parser handles non-UTF-8 files by falling back to latin-1."""

    def test_csv_parser_encoding_fallback(self):
        # Write CSV content in latin-1 (non-UTF-8) encoding
        content = "Name,Language,Cost\nRésumé,Français,0.05".encode("latin-1")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".csv") as f:
            f.write(content)
            temp_path = f.name

        try:
            markdown_table = process_csv_local(temp_path)
            self.assertIn("Résumé", markdown_table)
            self.assertIn("Français", markdown_table)
        finally:
            os.unlink(temp_path)

    def test_txt_parser_encoding_fallback(self):
        # Write TXT content in latin-1 encoding
        content = "Islamic Knowledge Résumé".encode("latin-1")
        with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
            f.write(content)
            temp_path = f.name

        try:
            text = process_txt_local(temp_path)
            self.assertEqual(text, "Islamic Knowledge Résumé")
        finally:
            os.unlink(temp_path)


class ZipExportBundleTestCase(TestCase):
    """Verifies curation zip bundle generation, Unicode handling, path collisions, and user boundary checks."""

    def test_zip_bundle_unicode_and_collisions(self):
        import zipfile
        from io import BytesIO

        from django.contrib.auth.models import User

        from extractor.utils import generate_curated_zip_bundle

        user = User.objects.create_user(username="testuser_zip", password="password")

        # Create two documents that share the exact same meta slug (language/author/title)
        # title has Arabic characters to test allow_unicode=True and empty slug fallback
        doc1 = SourceDocument.objects.create(
            original_filename="doc1.pdf",
            file_hash="hash-1",
            title="العربية",
            author="العربية",
            language="العربية",
            status="COMPLETED",
            refined_markdown="Doc 1 Content",
            uploaded_by=user,
        )
        doc2 = SourceDocument.objects.create(
            original_filename="doc2.pdf",
            file_hash="hash-2",
            title="العربية",
            author="العربية",
            language="العربية",
            status="COMPLETED",
            refined_markdown="Doc 2 Content",
            uploaded_by=user,
        )

        # Doc with empty slug symbols
        doc3 = SourceDocument.objects.create(
            original_filename="doc3.pdf",
            file_hash="hash-3",
            title="!!!",
            author="!!!",
            language="!!!",
            status="COMPLETED",
            refined_markdown="Doc 3 Content",
            uploaded_by=user,
        )

        zip_bytes = generate_curated_zip_bundle([doc1.id, doc2.id, doc3.id], user=user)
        self.assertTrue(len(zip_bytes) > 0)

        # Inspect Zip structure
        with zipfile.ZipFile(BytesIO(zip_bytes), "r") as z:
            namelist = z.namelist()
            # Verify Unicode preservation
            # slugify("العربية", allow_unicode=True) -> "العربية"
            self.assertTrue(any("العربية" in name for name in namelist))
            # Verify sequential batch indexing (e.g. Doc 2 has 002_ prefix)
            self.assertTrue(any("002_" in name for name in namelist))

    def test_zip_bundle_user_isolation(self):
        from django.contrib.auth.models import User

        from extractor.utils import generate_curated_zip_bundle

        user1 = User.objects.create_user(username="user1_zip", password="password")
        user2 = User.objects.create_user(username="user2_zip", password="password")

        doc1 = SourceDocument.objects.create(
            original_filename="user1.pdf",
            file_hash="h1",
            title="User 1 Doc",
            status="COMPLETED",
            uploaded_by=user1,
        )
        SourceDocument.objects.create(
            original_filename="user2.pdf",
            file_hash="h2",
            title="User 2 Doc",
            status="COMPLETED",
            uploaded_by=user2,
        )

        # Generating bundle as user2 selecting both documents
        # Should raise ValueError since doc1 is excluded by user boundary, leaving only doc2
        # If user2 exports only doc2, it should succeed, but let's test isolation:
        # If user2 tries to export doc1, it should raise ValueError
        with self.assertRaises(ValueError):
            generate_curated_zip_bundle([doc1.id], user=user2)

    def test_generate_sft_dataset_pairs_and_jsonl(self):
        from extractor.models import SourceDocument
        from extractor.utils import generate_sft_dataset_pairs, generate_sft_jsonl_bundle

        doc = SourceDocument.objects.create(
            original_filename="sft_test.txt",
            file_hash="sft-hash-12345",
            title="Tafsir Book",
            author="Scholar",
            language="Arabic",
            refined_markdown="## Page 1\n### Surah Al-Fatiha\nIn the name of God, Most Gracious, Most Merciful.",
            status="COMPLETED",
        )

        pairs = generate_sft_dataset_pairs([doc.id], limit=5)
        self.assertGreaterEqual(len(pairs), 1)
        first_pair = pairs[0]
        self.assertIn("prompt", first_pair)
        self.assertIn("completion", first_pair)
        self.assertIn("messages", first_pair)
        self.assertEqual(len(first_pair["messages"]), 3)
        self.assertEqual(first_pair["metadata"]["title"], "Tafsir Book")

        jsonl_bytes = generate_sft_jsonl_bundle([doc.id])
        self.assertIsInstance(jsonl_bytes, bytes)
        self.assertIn(b"Tafsir Book", jsonl_bytes)
        self.assertIn(b"messages", jsonl_bytes)


class LLMGatewayBackoffTestCase(TestCase):
    """Verifies that the LLM gateway exponential and dynamic backoff works properly."""

    def test_extract_retry_delay_from_error_msg(self):
        from extractor.llm_gateway import extract_retry_delay

        # Test "Please retry in X.XXs" pattern
        msg1 = "ClientError: 429 RESOURCE_EXHAUSTED. Please retry in 14.059141309s."
        self.assertEqual(extract_retry_delay(Exception(msg1)), 14.059141309)

        # Test json-like "retryDelay": "XXs" pattern
        msg2 = "{'error': {'code': 429, 'details': [{'@type': 'RetryInfo', 'retryDelay': '14s'}]}}"
        self.assertEqual(extract_retry_delay(Exception(msg2)), 14.0)

        # Test json-like "retryDelay": "XX" (without s)
        msg3 = '{"error": {"code": 429, "details": [{"retryDelay": "15"}]}}'
        self.assertEqual(extract_retry_delay(Exception(msg3)), 15.0)

        # Test no delay info
        msg4 = "429 Resource exhausted: Quota exceeded."
        self.assertIsNone(extract_retry_delay(Exception(msg4)))

    @patch("time.sleep")
    def test_execute_with_backoff_handles_429_suggested_delay(self, mock_sleep):
        from extractor.llm_gateway import execute_with_backoff

        mock_func = MagicMock()
        # Simulate rate limit on first call, success on second call
        err = Exception("429 RESOURCE_EXHAUSTED. Please retry in 12.5s.")
        mock_func.side_effect = [err, "Success Response"]

        res = execute_with_backoff(mock_func, max_retries=3, initial_delay=2)

        self.assertEqual(res, "Success Response")
        self.assertEqual(mock_func.call_count, 2)
        # Verify it slept for suggested delay (12.5) + safety margin (1.5) = 14.0s
        mock_sleep.assert_called_once_with(14.0)

    @patch("time.sleep")
    def test_execute_with_backoff_fallback_to_exponential(self, mock_sleep):
        from extractor.llm_gateway import execute_with_backoff

        mock_func = MagicMock()
        # Simulate rate limit without delay info, then success
        err = Exception("429 RESOURCE_EXHAUSTED. Quota exceeded.")
        mock_func.side_effect = [err, "Success Response"]

        res = execute_with_backoff(mock_func, max_retries=3, initial_delay=5)

        self.assertEqual(res, "Success Response")
        self.assertEqual(mock_func.call_count, 2)
        # Verify it slept for the initial_delay = 5s
        mock_sleep.assert_called_once_with(5)

    def test_execute_with_backoff_raises_non_rate_limit(self):
        from extractor.llm_gateway import execute_with_backoff

        mock_func = MagicMock()
        # Simulate a 400 Bad Request error
        err = Exception("400 Bad Request. Invalid prompt.")
        mock_func.side_effect = [err]

        with self.assertRaises(Exception) as context:
            execute_with_backoff(mock_func, max_retries=3)

        self.assertEqual(str(context.exception), "400 Bad Request. Invalid prompt.")
        self.assertEqual(mock_func.call_count, 1)


class LLMGatewayVertexFallbackTestCase(TestCase):
    """Verifies that the LLM gateway successfully and gracefully falls back to Vertex AI upon AI Studio quota exhaustion."""

    def test_is_rate_limit_error(self):
        from extractor.llm_gateway import is_rate_limit_error

        # APIError with 429
        err1 = Exception("Some message")
        err1.status_code = 429
        self.assertTrue(is_rate_limit_error(err1))

        # APIError with code = 429
        err2 = Exception("Some message")
        err2.code = 429
        self.assertTrue(is_rate_limit_error(err2))

        # Error message containing "resource_exhausted"
        self.assertTrue(is_rate_limit_error(Exception("Error: resource_exhausted")))
        self.assertTrue(is_rate_limit_error(Exception("Quota exceeded")))
        self.assertTrue(is_rate_limit_error(Exception("429 RESOURCE_EXHAUSTED")))

        # Normal non-rate-limit error
        self.assertFalse(is_rate_limit_error(Exception("400 Bad Request")))
        self.assertFalse(is_rate_limit_error(Exception("500 Internal Server Error")))

    @patch("extractor.llm_gateway.settings")
    @patch("extractor.llm_gateway.os.getenv")
    @patch("google.genai.Client")
    def test_get_vertex_client_success(self, mock_client_init, mock_getenv, mock_settings):
        from extractor.llm_gateway import get_vertex_client

        # Mock settings and env to provide project and location
        mock_settings.GCP_PROJECT = "my-test-project"
        mock_settings.GCP_REGION = "us-east4"
        mock_settings.VERTEX_API_KEY = None
        mock_getenv.side_effect = lambda key: None

        client = get_vertex_client()
        self.assertIsNotNone(client)
        mock_client_init.assert_called_once_with(vertexai=True, project="my-test-project", location="us-east4")

    @patch("extractor.llm_gateway.settings")
    @patch("extractor.llm_gateway.os.getenv")
    @patch("google.genai.Client")
    def test_get_vertex_client_success_with_api_key(self, mock_client_init, mock_getenv, mock_settings):
        from extractor.llm_gateway import get_vertex_client

        # Mock settings and env to provide project and location
        mock_settings.GCP_PROJECT = "my-test-project"
        mock_settings.GCP_REGION = "us-east4"
        mock_settings.VERTEX_API_KEY = "my-vertex-api-key"
        mock_getenv.side_effect = lambda key: None

        client = get_vertex_client()
        self.assertIsNotNone(client)
        mock_client_init.assert_called_once_with(vertexai=True, project="my-test-project", location="us-east4")

    @patch("extractor.llm_gateway.settings")
    @patch("extractor.llm_gateway.os.getenv")
    def test_get_vertex_client_missing_config(self, mock_getenv, mock_settings):
        from extractor.llm_gateway import get_vertex_client

        # Mock settings and env to not provide project
        mock_settings.GCP_PROJECT = None
        mock_getenv.return_value = None

        client = get_vertex_client()
        self.assertIsNone(client)

    @patch("extractor.llm_gateway.settings")
    @patch("extractor.llm_gateway.os.getenv")
    @patch("google.genai.Client")
    def test_get_vertex_client_uses_gcp_project_id(self, mock_client_init, mock_getenv, mock_settings):
        from extractor.llm_gateway import get_vertex_client

        mock_settings.GCP_PROJECT = None
        mock_settings.GCP_REGION = "asia-southeast1"
        mock_getenv.side_effect = lambda key: "project-from-gcp-id" if key == "GCP_PROJECT_ID" else None

        client = get_vertex_client()

        self.assertIsNotNone(client)
        mock_client_init.assert_called_once_with(
            vertexai=True, project="project-from-gcp-id", location="asia-southeast1"
        )

    @patch("extractor.llm_gateway.get_vertex_client_for_location")
    @patch("time.sleep")
    def test_execute_generate_content_with_fallback_cascades_to_vertex(
        self, mock_sleep, mock_get_vertex_client_for_location
    ):
        from extractor.llm_gateway import execute_generate_content_with_fallback

        # Mock AI Studio client to raise a 429 Rate Limit error
        mock_ai_studio_client = MagicMock()
        err_429 = Exception("429 RESOURCE_EXHAUSTED")
        mock_ai_studio_client.models.generate_content.side_effect = err_429

        # Mock Vertex Client to succeed
        mock_vertex_client = MagicMock()
        mock_vertex_response = MagicMock()
        mock_vertex_client.models.generate_content.return_value = mock_vertex_response
        mock_get_vertex_client_for_location.return_value = mock_vertex_client

        response, model_used = execute_generate_content_with_fallback(
            client=mock_ai_studio_client,
            model_name="gemini-2.5-flash",
            contents=["Hello world"],
        )

        self.assertEqual(response, mock_vertex_response)
        self.assertEqual(model_used, "gemini-2.5-flash")
        self.assertEqual(mock_ai_studio_client.models.generate_content.call_count, 5)
        mock_vertex_client.models.generate_content.assert_called_once()

    @patch("extractor.llm_gateway.get_vertex_client_for_location")
    @patch("time.sleep")
    def test_execute_generate_content_with_fallback_translates_files_for_vertex(
        self, mock_sleep, mock_get_vertex_client_for_location
    ):
        from extractor.llm_gateway import execute_generate_content_with_fallback

        # Create a mock File object
        class File:
            def __init__(self, uri, name):
                self.uri = uri
                self.name = name

        mock_file = File(uri="https://api.studio.google/files/123", name="test-file")

        # Mock AI Studio client to fail with 429
        mock_ai_studio_client = MagicMock()
        mock_ai_studio_client.models.generate_content.side_effect = Exception("429 RESOURCE_EXHAUSTED")

        # Mock Vertex Client
        mock_vertex_client = MagicMock()
        mock_vertex_response = MagicMock()
        mock_vertex_client.models.generate_content.return_value = mock_vertex_response
        mock_get_vertex_client_for_location.return_value = mock_vertex_client

        # Create a temporary file to load bytes from
        with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as f:
            f.write(b"PDF-1.5 mock data")
            temp_path = f.name

        try:
            with patch("google.genai.types.Part.from_bytes") as mock_from_bytes:
                from google.genai import types

                mock_part = types.Part(inline_data=types.Blob(data=b"PDF-1.5 mock data", mime_type="application/pdf"))
                mock_from_bytes.return_value = mock_part

                response, _ = execute_generate_content_with_fallback(
                    client=mock_ai_studio_client,
                    model_name="gemini-2.5-flash",
                    contents=[mock_file, "Explain this pdf"],
                    file_path_for_vertex=temp_path,
                )

                self.assertEqual(response, mock_vertex_response)
                mock_from_bytes.assert_called_once_with(data=b"PDF-1.5 mock data", mime_type="application/pdf")

                # Check contents sent to vertex client
                called_args, called_kwargs = mock_vertex_client.models.generate_content.call_args
                sent_contents = called_kwargs.get("contents") or called_args[1]
                self.assertEqual(sent_contents[0], mock_part)
                self.assertEqual(sent_contents[1], "Explain this pdf")

        finally:
            os.unlink(temp_path)

    @patch("extractor.llm_gateway.settings")
    @patch("extractor.llm_gateway.get_vertex_client_for_location")
    @patch("google.genai.Client")
    @patch("time.sleep")
    def test_execute_embed_content_with_fallback_cascades(
        self, mock_sleep, mock_client_init, mock_get_vertex_client_for_location, mock_settings
    ):
        mock_settings.GEMINI_API_KEY = "valid-api-key"
        from extractor.llm_gateway import execute_embed_content_with_fallback

        # Mock Vertex Client to fail with 429 (forces fallback to AI Studio)
        mock_vertex_client = MagicMock()
        mock_vertex_client.models.embed_content.side_effect = Exception("429 RESOURCE_EXHAUSTED")
        mock_get_vertex_client_for_location.return_value = mock_vertex_client

        # Mock AI Studio client to succeed
        mock_ai_studio_client = MagicMock()
        mock_ai_studio_response = MagicMock()
        mock_ai_studio_client.models.embed_content.return_value = mock_ai_studio_response
        mock_client_init.return_value = mock_ai_studio_client

        # Execute embedding content
        response = execute_embed_content_with_fallback(
            model_name="text-embedding-004",
            contents=["some text to embed"],
        )

        self.assertEqual(response, mock_ai_studio_response)
        self.assertEqual(mock_vertex_client.models.embed_content.call_count, 20)
        mock_ai_studio_client.models.embed_content.assert_called_once()


class UrlSchemeValidationTestCase(TestCase):
    """Verifies URL scheme validation logic under different environments."""

    def test_url_scheme_debug_mode_https(self):
        # Under settings.DEBUG = True, https:// should validate successfully
        with self.settings(DEBUG=True):
            try:
                validate_url_scheme("https://example.com")
            except ValueError as exc:
                self.fail(f"validate_url_scheme raised ValueError unexpectedly: {exc}")

    def test_url_scheme_debug_mode_http(self):
        # Under settings.DEBUG = True, http:// should validate successfully
        with self.settings(DEBUG=True):
            try:
                validate_url_scheme("http://example.com")
            except ValueError as exc:
                self.fail(f"validate_url_scheme raised ValueError unexpectedly: {exc}")

    def test_url_scheme_production_mode_https(self):
        # Under settings.DEBUG = False, https:// should validate successfully
        with self.settings(DEBUG=False):
            try:
                validate_url_scheme("https://example.com")
            except ValueError as exc:
                self.fail(f"validate_url_scheme raised ValueError unexpectedly: {exc}")

    def test_url_scheme_production_mode_http(self):
        # Under settings.DEBUG = False, http:// is insecure and should raise ValueError
        with self.settings(DEBUG=False):
            with self.assertRaises(ValueError) as ctx:
                validate_url_scheme("http://example.com")
            self.assertEqual(str(ctx.exception), "Insecure URL scheme. Production environments require https.")

    def test_url_scheme_invalid_prefix(self):
        # Schemes other than http/https should raise ValueError in all modes
        with self.settings(DEBUG=True):
            with self.assertRaises(ValueError) as ctx:
                validate_url_scheme("ftp://example.com")
            self.assertEqual(str(ctx.exception), "Invalid URL scheme. Only http and https schemes are permitted.")

        with self.settings(DEBUG=False):
            with self.assertRaises(ValueError) as ctx:
                validate_url_scheme("ftp://example.com")
            self.assertEqual(str(ctx.exception), "Invalid URL scheme. Only http and https schemes are permitted.")


class ArabicLayoutTestCase(TestCase):
    """Tests the Arabic and RTL typography layout parser."""

    def test_parse_arabic_layout_detection(self):
        from extractor.file_utils import parse_arabic_layout

        arabic_html = "<p>مرحبا بكم في منصة إيثير أومني</p>"
        result = parse_arabic_layout(arabic_html)
        self.assertIn('dir="rtl"', result)
        self.assertIn('class="arabic-text"', result)

    def test_parse_arabic_layout_latin_passthrough(self):
        from extractor.file_utils import parse_arabic_layout

        english_html = "<p>Welcome to AetherOmni Platform</p>"
        result = parse_arabic_layout(english_html)
        self.assertNotIn('dir="rtl"', result)
        self.assertNotIn('class="arabic-text"', result)


class GoogleOIDCTestCase(TestCase):
    """Tests GCP metadata server OIDC ID token retrieval and debug bypass."""

    def test_oidc_token_debug_mode_bypass(self):
        from extractor.file_utils import get_google_oidc_token

        with self.settings(DEBUG=True):
            token = get_google_oidc_token("https://worker.internal")
            self.assertIsNone(token)

    @patch("urllib.request.urlopen")
    def test_oidc_token_production_retrieval(self, mock_urlopen):
        from extractor.file_utils import get_google_oidc_token

        mock_resp = MagicMock()
        mock_resp.read.return_value = b"eySampleToken12345\n"
        mock_resp.__enter__.return_value = mock_resp
        mock_urlopen.return_value = mock_resp

        with self.settings(DEBUG=False):
            token = get_google_oidc_token("https://worker.internal")
            self.assertEqual(token, "eySampleToken12345")


class SurrealDocsTestCase(TestCase):
    """Tests SurrealDB document retrieval helper."""

    @patch("extractor.surreal_db.get_documents")
    def test_get_surreal_docs_filtering(self, mock_get_docs):
        from django.contrib.auth.models import User

        from extractor.file_utils import _get_surreal_docs

        user = User.objects.create_user(username="surrealdocuser", password="password123")
        mock_get_docs.return_value = [
            {"id": "doc1", "title": "Doc 1", "uploaded_by_id": str(user.id), "status": "COMPLETED"},
            {"id": "doc2", "title": "Doc 2", "uploaded_by_id": "other_user", "status": "COMPLETED"},
        ]

        docs = _get_surreal_docs(["doc1", "doc2"], user, actor_id=str(user.id))
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0].title, "Doc 1")

    @patch("extractor.surreal_db.get_documents")
    def test_get_surreal_docs_staff_bypass(self, mock_get_docs):
        from django.contrib.auth.models import User

        from extractor.file_utils import _get_surreal_docs

        staff_user = User.objects.create_user(username="staffuser", password="password123", is_staff=True)
        mock_get_docs.return_value = [
            {"id": "doc1", "title": "Doc 1", "uploaded_by_id": "user1", "status": "COMPLETED"},
            {"id": "doc2", "title": "Doc 2", "uploaded_by_id": "user2", "status": "COMPLETED"},
        ]

        docs = _get_surreal_docs(["doc1", "doc2"], staff_user)
        self.assertEqual(len(docs), 2)

    @patch("extractor.surreal_db.get_documents")
    def test_get_surreal_docs_ignores_incomplete(self, mock_get_docs):
        from django.contrib.auth.models import User

        from extractor.file_utils import _get_surreal_docs

        user = User.objects.create_user(username="normaluser", password="password123")
        mock_get_docs.return_value = [
            {"id": "doc1", "title": "Pending Doc", "uploaded_by_id": str(user.id), "status": "PENDING"},
            {"id": "doc2", "title": "Failed Doc", "uploaded_by_id": str(user.id), "status": "FAILED"},
        ]

        docs = _get_surreal_docs(["doc1", "doc2"], user, actor_id=str(user.id))
        self.assertEqual(len(docs), 0)


class RegionalModelTestCase(TestCase):
    """Tests dynamic cheapest regional Gemini model resolution."""

    def test_cheapest_regional_model_text(self):
        from extractor.llm_gateway import get_cheapest_regional_gemini_model

        model = get_cheapest_regional_gemini_model(region="asia-southeast1", is_vision=False)
        self.assertTrue(model in {"gemini-2.5-flash-lite", "gemini-2.5-flash"})

    def test_cheapest_regional_model_vision(self):
        from extractor.llm_gateway import get_cheapest_regional_gemini_model

        model = get_cheapest_regional_gemini_model(region="asia-southeast1", is_vision=True)
        self.assertTrue(model in {"gemini-2.5-flash", "gemini-2.5-flash-lite"})
