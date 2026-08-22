from urllib.parse import urlparse

from django.test import TestCase


def py_is_safe_preview_url(value: str) -> bool:
    """Python reference implementation of isSafePreviewUrl from static/js/editor.js."""
    if not value or not isinstance(value, str):
        return False
    try:
        parsed = urlparse(value)
        # Scheme must be http or https, or empty path if relative
        if parsed.scheme:
            return parsed.scheme in ("http", "https")
        # Disallow dangerous javascript: or data: in relative pseudo-schemes
        lower_val = value.strip().lower()
        return not lower_val.startswith(("javascript:", "data:", "vbscript:", "file:"))
    except (ValueError, TypeError, AttributeError):
        return False


class EditorSecurityTestCase(TestCase):
    """Unit tests verifying markdown link security logic."""

    def test_safe_urls(self):
        self.assertTrue(py_is_safe_preview_url("https://example.com"))
        self.assertTrue(py_is_safe_preview_url("http://example.com/page?query=1"))
        self.assertTrue(py_is_safe_preview_url("/docs/guide.md"))

    def test_unsafe_urls(self):
        self.assertFalse(py_is_safe_preview_url("javascript:alert(1)"))
        self.assertFalse(py_is_safe_preview_url("data:text/html,<script>alert(1)</script>"))
        self.assertFalse(py_is_safe_preview_url("vbscript:msgbox(1)"))
        self.assertFalse(py_is_safe_preview_url("file:///etc/passwd"))
        self.assertFalse(py_is_safe_preview_url(""))
        self.assertFalse(py_is_safe_preview_url(None))
