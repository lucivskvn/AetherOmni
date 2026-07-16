import io
import zipfile
from unittest.mock import MagicMock, patch

from django.test import TestCase

from extractor import file_utils
from extractor.models import SourceDocument


class FileUtilsTestCase(TestCase):
    """Direct unit tests for file_utils.py functions."""

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
            file_utils._process_zip_doc(
                idx=0,
                doc=doc,
                seen_lang_paths=seen_lang_paths,
                seen_author_paths=seen_author_paths,
                manifest=manifest,
                master_content=master_content,
                zip_file=zip_file,
            )

        # Check path additions
        self.assertEqual(len(seen_lang_paths), 1)
        self.assertEqual(len(seen_author_paths), 1)
        self.assertEqual(len(manifest["documents"]), 1)
        self.assertIn("Introduction", "".join(master_content))

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
