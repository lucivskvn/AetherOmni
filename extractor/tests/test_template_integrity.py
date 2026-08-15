import os
import re
from pathlib import Path

from django.conf import settings
from django.template.loader import get_template
from django.test import TestCase
from django.urls import get_resolver

STATIC_PATTERN = re.compile(r"{%\s*static\s+['\"]([^'\"]+)['\"]\s*(?:as\s+\w+)?\s*%}")
URL_PATTERN = re.compile(r"{%\s*url\s+['\"]([^'\"]+)['\"]")
EXTERNAL_SCRIPT_PATTERN = re.compile(r"<script[^>]+src=[\"'](https?://[^\"']+)[\"'][^>]*>", re.IGNORECASE)


class TemplateIntegrityTestCase(TestCase):
    """Automated QA Gate for Django templates, static assets, and URL route integrity."""

    def setUp(self):
        self.templates_dir = Path(settings.BASE_DIR) / "extractor" / "templates"
        self.static_dir = Path(settings.BASE_DIR) / "static"
        self.html_files = []
        for root, _, files in os.walk(self.templates_dir):
            for file in files:
                if file.endswith(".html"):
                    self.html_files.append(Path(root) / file)

    def test_static_asset_references_exist(self):
        """Ensure every {% static 'path' %} referenced in templates actually exists."""
        missing = []
        for path in self.html_files:
            content = path.read_text(encoding="utf-8")
            for match in STATIC_PATTERN.finditer(content):
                asset_path = match.group(1).split("?")[0]
                expected_file = self.static_dir / asset_path
                if not expected_file.exists():
                    missing.append(f"{path.name} -> {asset_path}")
        self.assertEqual(missing, [], f"Missing referenced static assets: {missing}")

    def test_template_url_routes_are_valid(self):
        """Ensure every {% url 'name' %} in templates maps to a registered Django URL."""
        resolver = get_resolver()
        registered_url_names = set(resolver.reverse_dict.keys())
        invalid_urls = []
        for path in self.html_files:
            content = path.read_text(encoding="utf-8")
            for match in URL_PATTERN.finditer(content):
                url_name = match.group(1)
                if url_name not in registered_url_names:
                    invalid_urls.append(f"{path.name} -> {url_name}")
        self.assertEqual(invalid_urls, [], f"Invalid URL names referenced in templates: {invalid_urls}")

    def test_external_scripts_have_sri_integrity(self):
        """Ensure external scripts use Subresource Integrity hashes."""
        allowed_unhashed = ("challenges.cloudflare.com/turnstile",)
        missing_sri = []
        for path in self.html_files:
            content = path.read_text(encoding="utf-8")
            for match in EXTERNAL_SCRIPT_PATTERN.finditer(content):
                tag = match.group(0)
                src = match.group(1)
                if any(allowed in src for allowed in allowed_unhashed):
                    continue
                if "integrity=" not in tag:
                    missing_sri.append(f"{path.name} -> {src}")
        self.assertEqual(missing_sri, [], f"External scripts missing SRI integrity: {missing_sri}")

    def test_templates_compile_and_render_without_syntax_errors(self):
        """Ensure all templates compile cleanly through the Django template engine."""
        for path in self.html_files:
            rel_name = str(path.relative_to(self.templates_dir))
            template = get_template(rel_name)
            self.assertIsNotNone(template)
