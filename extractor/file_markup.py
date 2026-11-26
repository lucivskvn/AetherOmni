# Copyright (c) 2026 KORDA Contributors.
#
# This file is part of KORDA.
#
# KORDA is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# KORDA is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with KORDA.  If not, see <https://www.gnu.org/licenses/>.

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import Any

import bleach  # type: ignore[import-untyped]
import markdown as md_lib  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)

_BLEACH_ALLOWED_TAGS = [
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
    "p",
    "b",
    "i",
    "strong",
    "em",
    "u",
    "ul",
    "ol",
    "li",
    "br",
    "hr",
    "table",
    "thead",
    "tbody",
    "tr",
    "th",
    "td",
    "code",
    "pre",
    "blockquote",
    "a",
    "span",
    "div",  # required for <div dir="rtl"> Arabic script wrappers
]
_BLEACH_ALLOWED_ATTRS = {
    "a": ["href", "title", "target"],
    "*": ["id", "class", "dir"],
}


def _add_noopener_rel(source):
    """Bleach token filter: add rel="noopener noreferrer" to <a> tags with target.

    bleach permits target= but strips rel: without noopener a target="_blank"
    link keeps window.opener access to this origin (reverse tabnabbing). Runs
    on parsed tokens, so attribute values containing '>', quotes, or the text
    'rel='/'target=' cannot confuse it.
    """
    for token in source:
        if token.get("type") == "StartTag" and token.get("name") == "a":
            data = token.get("data")
            if isinstance(data, dict) and any(attr == "target" for _, attr in data):
                data[(None, "rel")] = "noopener noreferrer"
        yield token


_BLEACH_CLEANER = bleach.Cleaner(
    tags=_BLEACH_ALLOWED_TAGS,
    attributes=_BLEACH_ALLOWED_ATTRS,
    protocols=["http", "https", "mailto"],
    strip=True,
    filters=[_add_noopener_rel],
)


def clean_html_content(raw_html: str) -> str:
    """
    Sanitizes HTML content using bleach to shield against XSS.
    This is ONLY called on already-compiled HTML, NOT on raw Markdown text.
    Preserves RTL wrappers (div, dir attribute) required for Arabic script rendering.
    """
    return _BLEACH_CLEANER.clean(raw_html)


def _detect_first_strong(text_content, latin_chars, arabic_chars):
    for char in text_content:
        if latin_chars.match(char):
            return "latin"
        if arabic_chars.match(char):
            return "arabic"
    return None


def _apply_arabic_attributes(attrs):

    if "dir=" in attrs:
        attrs = re.sub(r'dir="[^"]*"', 'dir="rtl"', attrs)
        attrs = re.sub(r"dir='[^']*'", 'dir="rtl"', attrs)
    else:
        attrs += ' dir="rtl"'

    if "class=" in attrs:
        attrs = re.sub(r'class="([^"]*)"', r'class="\1 arabic-text"', attrs)
        attrs = re.sub(r"class='([^']*)'", r"class='\1 arabic-text'", attrs)
    else:
        attrs += ' class="arabic-text"'
    return attrs


def parse_arabic_layout(html_content: str) -> str:
    """
    Parses HTML content, detects block tags whose first strong alphabetical
    character is Arabic, and adds dir="rtl" class="arabic-text" to them.
    Leaves English translations starting with English letters as LTR.
    """

    pattern = re.compile(
        r"<(p|li|blockquote|h1|h2|h3|h4|h5|h6|span|div|td)([^>]*)>(.*?)</\1>", re.DOTALL | re.IGNORECASE
    )

    arabic_chars = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF\uFB50-\uFDFF\uFE70-\uFEFF]")
    latin_chars = re.compile(r"[A-Za-z]")

    def replacer(match):
        tag_name = match.group(1)
        attrs = match.group(2)
        content = match.group(3)

        text_content = re.sub(r"<[^>]*>", "", content)
        first_strong = _detect_first_strong(text_content, latin_chars, arabic_chars)

        if first_strong == "arabic":
            attrs = _apply_arabic_attributes(attrs)

        return f"<{tag_name}{attrs}>{content}</{tag_name}>"

    return pattern.sub(replacer, html_content)


def render_markdown_to_html(markdown_text: str) -> str:
    """
    Converts Markdown to HTML safely and applies first-strong-character Arabic layout logic.
    """
    html = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code", "nl2br"])
    sanitized = clean_html_content(html)
    return parse_arabic_layout(sanitized)


def _escape_yaml_val(val: Any) -> str:
    s = str(val or "")
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\r", "\\r").replace("\n", "\\n")


def _build_yaml_frontmatter(doc: Any) -> str:
    """Build a YAML frontmatter header for export markdown files."""
    publisher = _escape_yaml_val(_get_doc_field_str(doc, "publisher"))
    publication_year = _escape_yaml_val(_get_doc_field_str(doc, "publication_year"))
    doi = _escape_yaml_val(_get_doc_field_str(doc, "doi"))
    license_type = _escape_yaml_val(_get_doc_field_str(doc, "license_type"))
    title = _escape_yaml_val(getattr(doc, "title", "") or "")
    author = _escape_yaml_val(getattr(doc, "author", "") or "")
    language = _escape_yaml_val(getattr(doc, "language", "") or "")
    doc_type = _escape_yaml_val(getattr(doc, "document_type", "") or "")
    source_hash = _escape_yaml_val(getattr(doc, "file_hash", "") or "")
    return (
        "---\n"
        f'title: "{title}"\n'
        f'author: "{author}"\n'
        f'language: "{language}"\n'
        f'document_type: "{doc_type}"\n'
        f'publisher: "{publisher}"\n'
        f'publication_year: "{publication_year}"\n'
        f'doi: "{doi}"\n'
        f'license_type: "{license_type}"\n'
        f'source_hash: "{source_hash}"\n'
        f'exported_at: "{datetime.now(UTC).isoformat()}"\n'
        "---\n\n"
    )


def _get_doc_field_str(doc: Any, attr: str, default: str = "") -> str:
    val = getattr(doc, attr, None)
    return str(val) if val is not None and val != "" else default
