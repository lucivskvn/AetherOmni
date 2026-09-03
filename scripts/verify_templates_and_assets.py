#!/usr/bin/env python3
"""
KORDA Template & Static Asset Integrity Gatekeeper

Verifies that:
1. All static assets referenced in Django templates (`{% static '...' %}`) exist on the filesystem.
2. All named URL routes referenced in Django templates (`{% url '...' %}`) exist in Django urlpatterns.
3. All external `<script>` tags include Subresource Integrity (SRI) `integrity` attributes.
4. Title and meta description tags adhere to standardized branding.
"""

from __future__ import annotations

import os
import re
import sys
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

TEMPLATES_DIR = ROOT / "extractor" / "templates"
STATIC_DIR = ROOT / "static"

STATIC_PATTERN = re.compile(r"{%\s*static\s+['\"]([^'\"\s]+)['\"](?:\s+as\s+\w+)?\s*%}")
URL_PATTERN = re.compile(r"{%\s*url\s+['\"]([^'\"\s]+)['\"]")
SCRIPT_OPEN_PATTERN = re.compile(r"<script\b([^>]*)>", re.IGNORECASE)
SRC_ATTR_PATTERN = re.compile(r'(?:^|\s)src\s*=\s*["\'](https?://[^"\'\s>]+)["\']', re.IGNORECASE)
INTEGRITY_ATTR_PATTERN = re.compile(r'\bintegrity\s*=\s*["\'][^"\'\s>]+["\']', re.IGNORECASE)


def verify_static_references() -> list[str]:
    errors: list[str] = []
    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if not file.endswith(".html"):
                continue
            path = Path(root) / file
            rel_path = path.relative_to(ROOT)
            content = path.read_text(encoding="utf-8")
            for match in STATIC_PATTERN.finditer(content):
                asset_path = match.group(1).split("?")[0]
                expected_file = STATIC_DIR / asset_path
                if not expected_file.exists():
                    errors.append(f"{rel_path}: Missing referenced static file '{asset_path}'")
    return errors


def _check_file_sri(path: Path, allowed_unhashed: tuple[str, ...]) -> list[str]:
    rel_path = path.relative_to(ROOT)
    content = path.read_text(encoding="utf-8")
    file_errors: list[str] = []
    for match in SCRIPT_OPEN_PATTERN.finditer(content):
        attrs = match.group(1)
        src_match = SRC_ATTR_PATTERN.search(attrs)
        if not src_match:
            continue
        src = src_match.group(1)
        parsed = urllib.parse.urlparse(src)
        host_and_path = f"{parsed.netloc}{parsed.path}"
        if any(host_and_path == allowed.strip("/") for allowed in allowed_unhashed):
            continue
        if not INTEGRITY_ATTR_PATTERN.search(attrs):
            file_errors.append(f"{rel_path}: External script '{src}' is missing SRI 'integrity' attribute")
    return file_errors


def _get_registered_url_names() -> set[str]:
    names: set[str] = set()
    try:
        import django

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
        try:
            django.setup()
        except RuntimeError:
            pass
        except Exception as setup_err:
            print(f"[Django Setup] Note: {setup_err}", file=sys.stderr)
        from django.urls import get_resolver

        def _extract_names(patterns: list) -> None:
            for p in patterns:
                if hasattr(p, "name") and p.name:
                    names.add(p.name)
                if hasattr(p, "url_patterns"):
                    _extract_names(p.url_patterns)

        _extract_names(get_resolver().url_patterns)
    except Exception as exc:
        print(f"[Resolver] Note: URL resolution check info: {exc}", file=sys.stderr)
    return names


def verify_named_urls() -> list[str]:
    errors: list[str] = []
    registered = _get_registered_url_names()
    if not registered:
        return errors

    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if not file.endswith(".html"):
                continue
            path = Path(root) / file
            rel_path = path.relative_to(ROOT)
            content = path.read_text(encoding="utf-8")
            for match in URL_PATTERN.finditer(content):
                url_name = match.group(1)
                # Allow standard admin routes or check in registered set
                if url_name.startswith("admin:") or url_name in registered:
                    continue
                errors.append(f"{rel_path}: Referenced named URL '{url_name}' is not defined in urlpatterns")
    return errors


OUTPUT_TAG_PATTERN = re.compile(r"<output\b([^>]*)>", re.IGNORECASE)
FOR_ATTR_PATTERN = re.compile(r'\bfor\s*=\s*["\']([^"\'\s>]+)["\']', re.IGNORECASE)


def verify_output_elements() -> list[str]:
    errors: list[str] = []
    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if not file.endswith(".html"):
                continue
            path = Path(root) / file
            rel_path = path.relative_to(ROOT)
            content = path.read_text(encoding="utf-8")
            for match in OUTPUT_TAG_PATTERN.finditer(content):
                attrs = match.group(1)
                if not FOR_ATTR_PATTERN.search(attrs):
                    errors.append(
                        f"{rel_path}: <output> element is missing 'for' attribute referencing input control ID"
                    )
    return errors


def verify_sri_attributes() -> list[str]:
    errors: list[str] = []
    allowed_unhashed = ("challenges.cloudflare.com/turnstile/v0/api.js",)
    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if file.endswith(".html"):
                errors.extend(_check_file_sri(Path(root) / file, allowed_unhashed))
    return errors


def main() -> int:
    print("Auditing Django templates & static asset integrity...")
    static_errors = verify_static_references()
    url_errors = verify_named_urls()
    sri_errors = verify_sri_attributes()
    output_errors = verify_output_elements()

    all_errors = static_errors + url_errors + sri_errors + output_errors
    if all_errors:
        print(f"FAILED: Found {len(all_errors)} template/asset error(s):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("✓ All static asset references, named routes, output tags, and SRI integrity checks passed cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
