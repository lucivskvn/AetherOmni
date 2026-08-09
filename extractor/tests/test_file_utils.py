import io
import zipfile
from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor import file_utils
from extractor.models import SourceDocument


class FileUtilsTestCase(TestCase):
    """Direct unit tests for file_utils.py functions."""

    def test_calculate_file_sha256(self):
        import os
        import tempfile

        content = b"Hello World"
        expected_hash = "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"

        # Test with file-like object
        file_obj = io.BytesIO(content)
        self.assertEqual(file_utils.calculate_file_sha256(file_obj), expected_hash)

        # Test with string path
        with tempfile.NamedTemporaryFile(delete=False) as temp_file:
            temp_file.write(content)
            temp_file_path = temp_file.name

        try:
            self.assertEqual(file_utils.calculate_file_sha256(temp_file_path), expected_hash)
        finally:
            os.remove(temp_file_path)

    def test_clean_html_content(self):
        dirty_html = "<script>alert('unsafe');</script><p>Safe content</p> <a href='javascript:void(0)'>Link</a>"
        clean = file_utils.clean_html_content(dirty_html)
        self.assertNotIn("script", clean)
        self.assertIn("Safe content", clean)
        self.assertNotIn("javascript", clean)

    def test_render_markdown_to_html(self):
        markdown_text = "# Header\nSome **bold** text."
        html = file_utils.render_markdown_to_html(markdown_text)
        self.assertIn("<h1>Header</h1>", html)
        self.assertIn("<strong>bold</strong>", html)

    @patch("extractor.file_utils.slugify")
    def test_process_zip_doc(self, mock_slugify):
        mock_slugify.side_effect = lambda x, **k: x.lower()
        seen_lang_paths = set()
        seen_author_paths = set()
        manifest = {"documents": []}
        master_content = []

        # Create a mock document
        doc = MagicMock(spec=SourceDocument)
        doc.id = 123
        doc.language = "Arabic"
        doc.author = "Ibn Khaldun"
        doc.title = "Muqaddimah"
        doc.document_type = "PDF"
        doc.original_filename = "muqaddimah.pdf"
        doc.page_count = 500
        doc.cost_usd = 0.05
        doc.file_hash = "abc123hash"
        doc.refined_markdown = "# Introduction"

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zip_file:
            ctx = file_utils.ZipExportContext(
                seen_lang_paths=seen_lang_paths,
                seen_author_paths=seen_author_paths,
                manifest=manifest,
                master_content=master_content,
                zip_file=zip_file,
            )
            file_utils._process_zip_doc(
                idx=0,
                doc=doc,
                ctx=ctx,
            )

        # Check path additions
        self.assertEqual(len(seen_lang_paths), 1)
        self.assertEqual(len(seen_author_paths), 1)
        self.assertEqual(len(manifest["documents"]), 1)
        self.assertIn("Introduction", "".join(master_content))

    def test_get_client_ip_with_x_forwarded_for(self):
        request = MagicMock()
        request.META = {"HTTP_X_FORWARDED_FOR": "192.168.1.1"}
        self.assertEqual(file_utils.get_client_ip(request), "192.168.1.1")

    def test_get_client_ip_with_x_forwarded_for_multiple(self):
        request = MagicMock()
        request.META = {"HTTP_X_FORWARDED_FOR": "192.168.1.1, 10.0.0.1, 127.0.0.1"}
        self.assertEqual(file_utils.get_client_ip(request), "192.168.1.1")

    def test_get_client_ip_with_remote_addr(self):
        request = MagicMock()
        request.META = {"REMOTE_ADDR": "10.0.0.1"}
        self.assertEqual(file_utils.get_client_ip(request), "10.0.0.1")

    def test_get_client_ip_none(self):
        request = MagicMock()
        request.META = {}
        self.assertEqual(file_utils.get_client_ip(request), "127.0.0.1")

    @patch("extractor.file_utils.slugify")
    @patch("extractor.models.SourceDocument.objects.filter")
    def test_generate_curated_zip_bundle(self, mock_filter, mock_slugify):
        mock_slugify.side_effect = lambda x, **k: x.lower()

        doc = MagicMock(spec=SourceDocument)
        doc.id = 456
        doc.language = "English"
        doc.author = "Shakespeare"
        doc.title = "Hamlet"
        doc.document_type = "TXT"
        doc.original_filename = "hamlet.txt"
        doc.page_count = 100
        doc.cost_usd = 0.01
        doc.file_hash = "xyz987hash"
        doc.refined_markdown = "To be or not to be"
        doc.raw_markdown = None

        mock_filter.return_value = [doc]

        zip_bytes = file_utils.generate_curated_zip_bundle([456])
        self.assertTrue(len(zip_bytes) > 0)

        # Read the generated zip
        zip_file = zipfile.ZipFile(io.BytesIO(zip_bytes))
        namelist = zip_file.namelist()
        self.assertTrue(any("Language/english" in name for name in namelist))
        self.assertTrue(any("Author/shakespeare" in name for name in namelist))
        self.assertIn("manifest.json", namelist)
        self.assertIn("master_archival_source.md", namelist)

    def test_validate_zip_bomb_protection(self):
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("test.txt", "hello")
        zip_buffer.seek(0)
        with zipfile.ZipFile(zip_buffer, "r") as zf:
            # Normal zip should pass
            file_utils.validate_zip(zf)

    def test_safe_extract_zip_slip_prevention(self):
        import tempfile

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w") as zf:
            zf.writestr("../evil.txt", "malicious payload")
        zip_buffer.seek(0)

        with tempfile.TemporaryDirectory() as tmp_dir, zipfile.ZipFile(zip_buffer, "r") as zf:
            with self.assertRaises(ValueError) as ctx:
                file_utils.safe_extract(zf, tmp_dir)
            self.assertIn("Zip Slip path traversal detected", str(ctx.exception))

    def test_cleanup_stale_temp_artifacts(self):
        import os
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Create a mock temporary file
            stale_file = os.path.join(tmp_dir, "aetheromni_test_stale.tmp")
            with open(stale_file, "w") as f:
                f.write("test")
            # Run cleanup with max_age_seconds=-1 to force removal
            removed = file_utils.cleanup_stale_temp_artifacts(temp_dir=tmp_dir, max_age_seconds=-1)
            self.assertEqual(removed, 1)
            self.assertFalse(os.path.exists(stale_file))

    @patch("extractor.llm_gateway.generate_multimodal_vision_ocr")
    def test_extract_pdf_diagrams_with_vision(self, mock_ocr):
        mock_ocr.return_value = "### Diagram Nodes: A -> B"
        res = file_utils.extract_pdf_diagrams_with_vision("/nonexistent/file.pdf")
        self.assertIsInstance(res, str)
