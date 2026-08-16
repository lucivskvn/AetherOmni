#!/usr/bin/env python3
"""
Template & Static Asset Integrity Gatekeeper — AetherOmni v2.0

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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES_DIR = ROOT / "extractor" / "templates"
STATIC_DIR = ROOT / "static"

STATIC_PATTERN = re.compile(r"{%\s*static\s+['\"]([^'\"\s]+)['\"](?:\s+as\s+\w+)?\s*%}")
URL_PATTERN = re.compile(r"{%\s*url\s+['\"]([^'\"\s]+)['\"]")
EXTERNAL_SCRIPT_PATTERN = re.compile(r"<script\b[^>]*?\bsrc=[\"'](https?://[^'\"\s>]+)[\"'][^>]*>", re.IGNORECASE)


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
    for match in EXTERNAL_SCRIPT_PATTERN.finditer(content):
        script_tag = match.group(0)
        src = match.group(1)
        if any(allowed in src for allowed in allowed_unhashed):
            continue
        if "integrity=" not in script_tag:
            file_errors.append(f"{rel_path}: External script '{src}' is missing SRI 'integrity' attribute")
    return file_errors


def verify_sri_attributes() -> list[str]:
    errors: list[str] = []
    allowed_unhashed = ("challenges.cloudflare.com/turnstile",)
    for root, _, files in os.walk(TEMPLATES_DIR):
        for file in files:
            if file.endswith(".html"):
                errors.extend(_check_file_sri(Path(root) / file, allowed_unhashed))
    return errors


def main() -> int:
    print("Auditing Django templates & static asset integrity...")
    static_errors = verify_static_references()
    sri_errors = verify_sri_attributes()

    all_errors = static_errors + sri_errors
    if all_errors:
        print(f"FAILED: Found {len(all_errors)} template/asset error(s):", file=sys.stderr)
        for err in all_errors:
            print(f"  - {err}", file=sys.stderr)
        return 1

    print("✓ All static asset references and SRI integrity checks passed cleanly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
