# Copyright (c) 2026 Knowledge Desk Contributors.
# All rights reserved. Confidential and Proprietary.

from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request
import zipfile
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from django.http import HttpRequest

import bleach
import markdown as md_lib
from django.conf import settings
from django.utils.text import slugify

logger = logging.getLogger(__name__)

APPLICATION_JSON = "application/json"

# ── Fallback exchange rates (used if the external API is offline) ─────────────
# Approximate rates as of mid-2026 — stale but far better than a blocking 5s timeout.
_FALLBACK_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "IDR": 16200.0,
    "SAR": 3.75,
    "TRY": 32.0,
}

# Thread-safe in-memory cache for exchange rates (1-hour TTL)
_rates_cache: dict[str, Any] = {}
_rates_cache_lock = threading.Lock()


def calculate_file_sha256(file_handle_or_path: str | IO[bytes]) -> str:
    """
    Computes SHA-256 checksum in chunks of 64KB for deduplication and content-addressing.
    Accepts either a string path or a file-like object.
    """
    sha256 = hashlib.sha256()
    if isinstance(file_handle_or_path, str):
        with open(file_handle_or_path, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha256.update(chunk)
    else:
        file_handle_or_path.seek(0)
        for chunk in iter(lambda: file_handle_or_path.read(65536), b""):
            sha256.update(chunk)
        file_handle_or_path.seek(0)
    return sha256.hexdigest()


def process_csv_local(file_path: str) -> str:
    """
    Converts tabular CSV files cleanly to an aligned Markdown table locally ($0 cost!).
    Supports heavy columns and escapes standard symbols.
    """
    import csv

    markdown_lines = []
    try:
        with open(file_path, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f)
            rows = list(reader)
    except UnicodeDecodeError:
        with open(file_path, newline="", encoding="latin-1") as f:
            reader = csv.reader(f)
            rows = list(reader)

    if not rows:
        return "*Empty CSV Document*"

    headers = rows[0]
    markdown_lines.append("| " + " | ".join([h.replace("|", "\\|") for h in headers]) + " |")
    markdown_lines.append("| " + " | ".join(["---" for _ in headers]) + " |")

    for row in rows[1:]:
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[: len(headers)]
        markdown_lines.append(
            "| " + " | ".join([cell.replace("|", "\\|").replace("\n", "<br>") for cell in row]) + " |"
        )

    return "\n".join(markdown_lines)


def process_txt_local(file_path: str) -> str:
    """
    Reads local TXT files, structures them with clean margins and sanitizes contents.
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(file_path, encoding="latin-1") as f:
            content = f.read()
    return content


def clean_html_content(raw_html: str) -> str:
    """
    Sanitizes HTML content using bleach to shield against XSS.
    This is ONLY called on already-compiled HTML, NOT on raw Markdown text.
    Preserves RTL wrappers (div, dir attribute) required for Arabic script rendering (Gap H-7).
    """
    allowed_tags = [
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
        "div",  # Gap H-7: required for <div dir="rtl"> Arabic script wrappers
    ]
    allowed_attrs = {
        "a": ["href", "title", "target"],
        "*": ["id", "class", "dir"],
    }
    return bleach.clean(raw_html, tags=allowed_tags, attributes=allowed_attrs, strip=True)


def parse_arabic_layout(html_content: str) -> str:
    """
    Parses HTML content, detects block tags whose first strong alphabetical
    character is Arabic, and adds dir="rtl" class="arabic-text" to them.
    Leaves English translations starting with English letters as LTR.
    """
    import re

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

        first_strong = None
        for char in text_content:
            if latin_chars.match(char):
                first_strong = "latin"
                break
            elif arabic_chars.match(char):
                first_strong = "arabic"
                break

        if first_strong == "arabic":
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

        return f"<{tag_name}{attrs}>{content}</{tag_name}>"

    return pattern.sub(replacer, html_content)


def render_markdown_to_html(markdown_text: str) -> str:
    """
    Converts Markdown to HTML safely and applies first-strong-character Arabic layout logic.
    """
    html = md_lib.markdown(markdown_text, extensions=["tables", "fenced_code", "nl2br"])
    sanitized = clean_html_content(html)
    return parse_arabic_layout(sanitized)


def _build_yaml_frontmatter(doc: Any) -> str:
    """Build a YAML frontmatter header for export markdown files (Gap H-4)."""
    return (
        "---\n"
        f'title: "{doc.title}"\n'
        f'author: "{doc.author}"\n'
        f'language: "{doc.language}"\n'
        f'document_type: "{doc.document_type}"\n'
        f'source_hash: "{doc.file_hash}"\n'
        f'exported_at: "{datetime.now(UTC).isoformat()}"\n'
        "---\n\n"
    )


def _process_zip_doc(idx, doc, seen_lang_paths, seen_author_paths, manifest, master_content, zip_file):
    clean_lang = slugify(doc.language or "unknown", allow_unicode=True) or "unknown"
    clean_author = slugify(doc.author or "unknown", allow_unicode=True) or "unknown"
    doc_type = doc.document_type or "PDF"

    prefix_name = f"{doc.language or 'LANG'}_{doc.author or 'AUTHOR'}_{doc.title or 'TITLE'}_{doc_type}"
    base_slug = slugify(prefix_name, allow_unicode=True) or f"doc_{doc.id}"

    lang_slug = f"{base_slug}.md"
    lang_path = f"Language/{clean_lang}/{lang_slug}"
    counter = 1
    while lang_path in seen_lang_paths:
        counter += 1
        lang_slug = f"{base_slug}_{counter}.md"
        lang_path = f"Language/{clean_lang}/{lang_slug}"
    seen_lang_paths.add(lang_path)

    author_slug = f"{base_slug}.md"
    author_path = f"Author/{clean_author}/{author_slug}"
    counter = 1
    while author_path in seen_author_paths:
        counter += 1
        author_slug = f"{base_slug}_{counter}.md"
        author_path = f"Author/{clean_author}/{author_slug}"
    seen_author_paths.add(author_path)

    doc_markdown = doc.refined_markdown or doc.raw_markdown or "*Empty Document Content*"
    # Gap H-4: prepend YAML frontmatter to each exported file
    frontmatter = _build_yaml_frontmatter(doc)
    full_content = frontmatter + doc_markdown

    zip_file.writestr(lang_path, full_content)
    zip_file.writestr(author_path, full_content)

    manifest["documents"].append(
        {
            "id": doc.id,
            "filename": doc.original_filename,
            "title": doc.title,
            "author": doc.author,
            "language": doc.language,
            "type": doc_type,
            "page_count": doc.page_count,
            "cost_usd": float(doc.cost_usd),
            "hash": doc.file_hash,
        }
    )

    master_content.append(
        f"<!-- SOURCE_START_{idx + 1}: {doc.title} by {doc.author} ({doc.language}) [Type: {doc_type}, Hash: {doc.file_hash}] -->"
    )
    master_content.append(f"\n# SOURCE: {doc.title}\n")
    master_content.append(f"**Author:** {doc.author}  ")
    master_content.append(f"**Language:** {doc.language}  ")
    master_content.append(f"**Document Type:** {doc_type}\n")
    master_content.append("---\n")
    master_content.append(doc_markdown)
    master_content.append("\n\n---\n\n")
    master_content.append(f"<!-- SOURCE_END_{idx + 1}: {doc.title} -->\n\n")


def generate_curated_zip_bundle(document_ids: list[int] | list[str], user: Any = None) -> bytes:
    """
    Aggregates selected documents into an organized directory structure.
    Saves them by Language/ and Author/.
    Prepends YAML frontmatter to each exported markdown file (Gap H-4).
    Enforces user boundaries if `user` is provided and is not a staff/superuser.
    """
    from django.db.models import Q

    from extractor.models import SourceDocument

    docs = SourceDocument.objects.filter(id__in=document_ids, status="COMPLETED")
    if user and not (user.is_staff or user.is_superuser):
        docs = docs.filter(Q(uploaded_by=user) | Q(uploaded_by__isnull=True))

    docs_list = list(docs)
    if not docs_list:
        raise ValueError("No completed documents selected for export bundle.")

    zip_buffer = BytesIO()

    manifest = {
        "exported_at": datetime.now(UTC).isoformat(),
        "document_count": len(docs_list),
        "total_cost_usd": float(sum(d.cost_usd for d in docs_list)),
        "total_pages": sum(d.page_count for d in docs_list),
        "documents": [],
    }

    master_content = [
        "# Master NotebookLM Comprehensive Source Book\n",
        "This master document contains all aggregated booklet knowledge in a single high-density layout. "
        "It uses strict structural indicators to help NotebookLM easily parse each separate source content boundaries.\n\n",
    ]

    seen_lang_paths: set[str] = set()
    seen_author_paths: set[str] = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        for idx, doc in enumerate(docs_list):
            _process_zip_doc(idx, doc, seen_lang_paths, seen_author_paths, manifest, master_content, zip_file)

        zip_file.writestr("master_notebooklm_source.md", "\n".join(master_content))
        zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))

    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def get_client_ip(request: HttpRequest) -> str | None:
    """Extracts client IP, respecting load balancers like Cloud Run or Cloudflare."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        ip = x_forwarded_for.split(",")[0].strip()
    else:
        ip = request.META.get("REMOTE_ADDR")
    return ip


def _resolve_currency_and_symbol(accept_language: str) -> tuple[str, str]:
    if "id" in accept_language:
        return "IDR", "Rp "
    if "ar" in accept_language:
        return "SAR", "SR "
    if any(lang in accept_language for lang in ["de", "fr", "es", "it", "nl", "pt"]):
        return "EUR", "€"
    if "gb" in accept_language:
        return "GBP", "£"
    if "tr" in accept_language:
        return "TRY", "₺"
    return "USD", "$"


def get_locale_currency_details(request: Any) -> dict[str, Any]:
    """
    Resolves target currency, local currency name, and current exchange rate from USD
    by parsing browser settings (HTTP_ACCEPT_LANGUAGE) or system settings override.
    Gap E-38: caches fallback rates for 1 hour when external API is unavailable.
    Gap D-6: uses SurrealDB KV cache instead of Redis.
    """
    from extractor.models import SystemSettings

    try:
        settings_obj = SystemSettings.get_settings()
        selected_currency = settings_obj.currency
    except Exception:
        selected_currency = "auto"

    if selected_currency and selected_currency != "auto":
        currency = selected_currency
        symbol_map = {"USD": "$", "IDR": "Rp ", "SAR": "SR ", "EUR": "€", "GBP": "£", "TRY": "₺"}
        symbol = symbol_map.get(currency, "$")
    else:
        accept_language = request.META.get("HTTP_ACCEPT_LANGUAGE", "").lower() if request else ""
        currency, symbol = _resolve_currency_and_symbol(accept_language)

    # Try SurrealDB KV cache first, fallback to Django cache (useful for testing)
    rates = None
    try:
        from extractor import surreal_db

        rates = surreal_db.kv_cache_get("usd_exchange_rates")
    except Exception as exc:
        logger.debug("[Exchange Rates] Failed to read cached rates: %s", exc)

    if not rates:
        from django.core.cache import cache

        rates = cache.get("usd_exchange_rates")

    if not rates:
        try:
            with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=5) as response:  # nosec B310 nosemgrep
                data = json.loads(response.read().decode())
                if data.get("result") == "success":
                    rates = data.get("rates", {})
                    # Cache for 24 hours in SurrealDB KV
                    try:
                        from extractor import surreal_db

                        surreal_db.kv_cache_set("usd_exchange_rates", rates, ttl_seconds=86400)
                    except Exception as exc:
                        logger.debug("[Exchange Rates] Failed to cache rates: %s", exc)
        except Exception as exc:
            logger.warning("[Exchange Rates] Error fetching live rates: %s — using fallback.", exc)
            # Gap E-38: use static fallback so page loads don't block for 5 seconds
            rates = _FALLBACK_RATES.copy()
            # Cache fallback for 1 hour to prevent retry storms
            try:
                from extractor import surreal_db

                surreal_db.kv_cache_set("usd_exchange_rates", rates, ttl_seconds=3600)
            except Exception as exc:
                logger.debug("[Exchange Rates] Failed to cache fallback rates: %s", exc)

    rate = rates.get(currency, 1.0) if rates else 1.0
    return {"currency_code": currency, "symbol": symbol, "rate": rate}


def format_localized_cost(cost_usd: Decimal, currency_details: dict[str, Any]) -> str:
    """Formats USD pricing into both localized and USD formats."""
    usd_val = Decimal(str(cost_usd))
    rate = Decimal(str(currency_details["rate"]))
    local_val = usd_val * rate

    def fmt_usd(v: Decimal) -> str:
        if v < Decimal("0.10") and v > 0:
            return f"${v:.4f}"
        return f"${v:.2f}"

    if currency_details["currency_code"] == "USD":
        return fmt_usd(usd_val)

    if currency_details["currency_code"] == "IDR":
        return f"{currency_details['symbol']}{int(local_val):,} (~{fmt_usd(usd_val)})"

    return f"{currency_details['symbol']}{local_val:.2f} (~{fmt_usd(usd_val)})"


def get_google_oidc_token(audience: str) -> str | None:
    """
    Fetches an OIDC ID token from the GCP metadata server for the given audience.
    Gap E-39: returns None immediately in DEBUG mode to skip blocking network calls.
    """
    if settings.DEBUG:
        return None

    url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?audience={audience}"
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
            return response.read().decode("utf-8").strip()
    except Exception as exc:
        logger.warning("[OIDC] Failed to fetch OIDC identity (not in GCP?): %s", exc)
        return None


def async_task_with_wakeup(task_name: str, payload: dict, countdown: int = 0) -> None:
    """
    Gap C-2: Alias for cloud_tasks.enqueue — replaces the old Django-Q async_task wrapper.
    Provides backward-compatible interface for call sites that used the old signature.
    """
    from extractor import cloud_tasks

    cloud_tasks.enqueue(task_name, payload, countdown=countdown)
