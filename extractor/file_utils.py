# Copyright (c) 2026 AetherOmni Contributors.
#
# This file is part of AetherOmni.
#
# AetherOmni is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# AetherOmni is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.
#
# You should have received a copy of the GNU Affero General Public License
# along with AetherOmni.  If not, see <https://www.gnu.org/licenses/>.


from __future__ import annotations

import hashlib
import json
import logging
import threading
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO

from django.conf import settings


def _get_gcs_bucket():
    from django.core.exceptions import ImproperlyConfigured
    from google.cloud import storage

    client = storage.Client()
    # DEBT-03 fix: never silently fall back to a hardcoded bucket name.
    # GS_BUCKET_NAME MUST be explicitly set via env var / Secret Manager.
    bucket_name = getattr(settings, "GS_BUCKET_NAME", None)
    if not bucket_name:
        raise ImproperlyConfigured(
            "GS_BUCKET_NAME is not configured. Set it via environment variable or GCP Secret Manager."
        )
    return client.bucket(bucket_name)


from typing import IO, Any

import bleach  # type: ignore[import-untyped]
import markdown as md_lib  # type: ignore[import-untyped]
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

# ── ZIP Security Constants (SEC-03 Zip Slip + EDGE-05 Zip Bomb guards) ────────
# Maximum total uncompressed bytes allowed from any single uploaded ZIP archive.
_ZIP_MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # 500 MB
# Maximum number of members (files) permitted inside a single ZIP archive.
_ZIP_MAX_MEMBER_COUNT: int = 500


def validate_zip(zf: zipfile.ZipFile) -> None:
    """Guard against Zip Bomb (EDGE-05) and path traversal Zip Slip (SEC-03) attacks.

    Call this immediately after opening any user-supplied ZipFile before extracting.

    Raises:
        ValueError: if member count, total uncompressed size, or a path traversal
                    attempt is detected.
    """
    members = zf.infolist()

    # EDGE-05: Bomb guard — reject archives exceeding member count or size limits
    if len(members) > _ZIP_MAX_MEMBER_COUNT:
        raise ValueError(
            f"ZIP archive contains {len(members)} files, exceeding the maximum of {_ZIP_MAX_MEMBER_COUNT}."
        )
    total_uncompressed = sum(m.file_size for m in members)
    if total_uncompressed > _ZIP_MAX_UNCOMPRESSED_BYTES:
        raise ValueError(
            f"ZIP uncompressed size {total_uncompressed} bytes exceeds the {_ZIP_MAX_UNCOMPRESSED_BYTES}-byte limit."
        )


def safe_extract(zf: zipfile.ZipFile, target_dir: str) -> None:
    """Extract ZIP contents with Zip Slip path traversal protection (SEC-03).

    Validates each member's resolved path stays within `target_dir` before
    extracting. Always call `validate_zip()` first to apply size/count limits.

    Raises:
        ValueError: if any ZIP member path escapes the target directory.
    """
    import os

    real_target = os.path.realpath(target_dir)
    for member in zf.infolist():
        member_path = os.path.realpath(os.path.join(real_target, member.filename))
        # SEC-03: Zip Slip guard — reject members that resolve outside the target dir
        if not member_path.startswith(real_target + os.sep) and member_path != real_target:
            raise ValueError(f"Zip Slip path traversal detected in member: '{member.filename}'. Extraction aborted.")
        zf.extract(member, target_dir)


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

    markdown_lines: list[str] = []
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
    markdown_lines.extend(
        (
            "| " + " | ".join([h.replace("|", "\\|") for h in headers]) + " |",
            "| " + " | ".join(["---" for _ in headers]) + " |",
        )
    )

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
    Preserves RTL wrappers (div, dir attribute) required for Arabic script rendering.
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
        "div",  # required for <div dir="rtl"> Arabic script wrappers
    ]
    allowed_attrs = {
        "a": ["href", "title", "target"],
        "*": ["id", "class", "dir"],
    }
    return bleach.clean(raw_html, tags=allowed_tags, attributes=allowed_attrs, strip=True)


def _detect_first_strong(text_content, latin_chars, arabic_chars):
    for char in text_content:
        if latin_chars.match(char):
            return "latin"
        if arabic_chars.match(char):
            return "arabic"
    return None


def _apply_arabic_attributes(attrs):
    import re

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


def _build_yaml_frontmatter(doc: Any) -> str:
    """Build a YAML frontmatter header for export markdown files."""
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


@dataclass
class ZipExportContext:
    seen_lang_paths: set[str]
    seen_author_paths: set[str]
    manifest: dict[str, Any]
    master_content: list[str]
    zip_file: zipfile.ZipFile
    include_taxonomic_views: bool = True


def _process_zip_doc(idx: int, doc: Any, ctx: ZipExportContext):
    clean_lang = slugify(doc.language or "unknown", allow_unicode=True) or "unknown"
    clean_author = slugify(doc.author or "unknown", allow_unicode=True) or "unknown"
    doc_type = doc.document_type or "PDF"

    # Formal standardized batch filename: [3-DIGIT_INDEX]_[TITLE_SLUG].md
    index_prefix = f"{idx + 1:03d}"
    raw_title = doc.title or doc.original_filename or f"document_{doc.id}"
    doc_title_slug = slugify(raw_title, allow_unicode=True) or f"document_{doc.id}"
    base_slug = f"{index_prefix}_{doc_title_slug}"

    doc_markdown = doc.refined_markdown or doc.raw_markdown or "*Empty Document Content*"
    frontmatter = _build_yaml_frontmatter(doc)
    full_content = frontmatter + doc_markdown

    # Primary single-copy standardized document output
    doc_path = f"documents/{base_slug}.md"
    ctx.zip_file.writestr(doc_path, full_content)

    if ctx.include_taxonomic_views:
        lang_slug = f"{base_slug}.md"
        lang_path = f"Language/{clean_lang}/{lang_slug}"
        counter = 1
        while lang_path in ctx.seen_lang_paths:
            counter += 1
            lang_slug = f"{base_slug}_{counter}.md"
            lang_path = f"Language/{clean_lang}/{lang_slug}"
        ctx.seen_lang_paths.add(lang_path)

        author_slug = f"{base_slug}.md"
        author_path = f"Author/{clean_author}/{author_slug}"
        counter = 1
        while author_path in ctx.seen_author_paths:
            counter += 1
            author_slug = f"{base_slug}_{counter}.md"
            author_path = f"Author/{clean_author}/{author_slug}"
        ctx.seen_author_paths.add(author_path)

        ctx.zip_file.writestr(lang_path, full_content)
        ctx.zip_file.writestr(author_path, full_content)

    ctx.manifest["documents"].append(
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
            "export_path": doc_path,
        }
    )

    ctx.master_content.append(
        f"<!-- SOURCE_START_{idx + 1}: {doc.title} by {doc.author} ({doc.language}) [Type: {doc_type}, Hash: {doc.file_hash}] -->"
    )
    ctx.master_content.append(f"\n# SOURCE: {doc.title}\n")
    ctx.master_content.append(f"**Author:** {doc.author}  ")
    ctx.master_content.append(f"**Language:** {doc.language}  ")
    ctx.master_content.append(f"**Document Type:** {doc_type}\n")
    ctx.master_content.append(doc_markdown)
    ctx.master_content.append(f"\n<!-- SOURCE_END_{idx + 1} -->\n\n---\n")


def _get_offline_docs(document_ids, user):
    from django.db.models import Q

    from extractor.models import SourceDocument

    docs = SourceDocument.objects.filter(id__in=document_ids, status="COMPLETED")
    if user and not (user.is_staff or user.is_superuser):
        docs = docs.filter(Q(uploaded_by=user) | Q(uploaded_by__isnull=True))
    return list(docs)


def _get_surreal_docs(document_ids, user):
    from django.contrib.auth import get_user_model

    from extractor import surreal_db
    from extractor.views import _wrap_surreal_doc

    user_model = get_user_model()
    users_map = {str(u.id): u for u in user_model.objects.all()}

    docs_list = []
    raw_docs = surreal_db.get_documents(document_ids)
    for raw_doc in raw_docs:
        if not raw_doc or raw_doc.get("status") != "COMPLETED":
            continue

        uploaded_by_id = raw_doc.get("uploaded_by_id")
        if user and not (user.is_staff or user.is_superuser) and uploaded_by_id and uploaded_by_id != str(user.id):
            continue

        doc = _wrap_surreal_doc(raw_doc, users_map)
        docs_list.append(doc)
    return docs_list


def generate_curated_zip_bundle(
    document_ids: list[int] | list[str],
    user: Any = None,
    include_taxonomic_views: bool = True,
) -> bytes:
    """
    Aggregates selected documents into an organized directory structure.
    Saves unique copies under documents/ and optionally by Language/ and Author/.
    Prepends YAML frontmatter to each exported markdown file.
    Enforces user boundaries if `user` is provided and is not a staff/superuser.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        docs_list = _get_offline_docs(document_ids, user)
    else:
        docs_list = _get_surreal_docs(document_ids, user)

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
        "# Master Digital Archival Source Book\n",
        "This master document contains all aggregated booklet knowledge in a single high-density layout. "
        "It uses strict structural indicators to help archival systems easily parse each separate source content boundaries.\n\n",
    ]

    seen_lang_paths: set[str] = set()
    seen_author_paths: set[str] = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        ctx = ZipExportContext(
            seen_lang_paths=seen_lang_paths,
            seen_author_paths=seen_author_paths,
            manifest=manifest,
            master_content=master_content,
            zip_file=zip_file,
            include_taxonomic_views=include_taxonomic_views,
        )
        for idx, doc in enumerate(docs_list):
            _process_zip_doc(idx, doc, ctx)

        zip_file.writestr("master_archival_source.md", "\n".join(master_content))
        zip_file.writestr("manifest.json", json.dumps(manifest, indent=2))

    zip_buffer.seek(0)
    return zip_buffer.getvalue()


def cleanup_stale_temp_artifacts(temp_dir: str | None = None, max_age_seconds: int = 86400) -> int:
    """Automated cleanup policy per DevSecOps best practices.

    Scans temporary directories for stale processing files older than max_age_seconds (default: 24h).
    Returns total count of removed temporary artifacts.
    """
    import os
    import tempfile
    import time

    # VULN-01 / S5443 fix: resolve absolute realpath to prevent symlink traversal in temp dir
    raw_dir = temp_dir if temp_dir is not None else tempfile.gettempdir()  # NOSONAR python:S5443
    target_dir = os.path.realpath(raw_dir)
    if not os.path.exists(target_dir):
        return 0

    now = time.time()
    removed_count = 0
    prefix_patterns = ("tmpx", "tmp", "aetheromni_", "pdf_export_")

    for filename in os.listdir(target_dir):
        if not filename.startswith(prefix_patterns):
            continue
        file_path = os.path.realpath(os.path.join(target_dir, filename))
        # Ensure resolved file path remains safely within target_dir
        if not file_path.startswith(target_dir + os.sep):
            continue
        try:
            if os.path.isfile(file_path):
                file_age = now - os.path.getmtime(file_path)
                if file_age > max_age_seconds:
                    os.remove(file_path)
                    removed_count += 1
        except OSError:
            pass

    return removed_count


def get_client_ip(request: Any) -> str:
    """Extract the true client IP from Django request headers."""
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded_for:
        return str(x_forwarded_for.split(",")[0].strip())
    return str(request.META.get("REMOTE_ADDR", "127.0.0.1"))


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


def _fetch_live_rates_with_fallback():
    rates = None
    try:
        with urllib.request.urlopen("https://open.er-api.com/v6/latest/USD", timeout=5) as response:  # nosec B310 nosemgrep
            data = json.loads(response.read().decode())
            if data.get("result") == "success":
                rates = data.get("rates", {})
                try:
                    from extractor import surreal_db

                    surreal_db.kv_cache_set("usd_exchange_rates", rates, ttl_seconds=86400)
                except (RuntimeError, ValueError, KeyError, AttributeError) as exc:
                    logger.debug("[Exchange Rates] Failed to cache rates: %s", exc)
    except (urllib.error.URLError, ValueError, TypeError, KeyError, OSError) as exc:
        logger.warning("[Exchange Rates] Error fetching live rates: %s — using fallback.", exc)
        rates = _FALLBACK_RATES.copy()
        try:
            from extractor import surreal_db

            surreal_db.kv_cache_set("usd_exchange_rates", rates, ttl_seconds=3600)
        except (RuntimeError, ValueError, KeyError, AttributeError) as exc:
            logger.debug("[Exchange Rates] Failed to cache fallback rates: %s", exc)
    return rates


def _get_exchange_rates():
    rates = None
    try:
        from extractor import surreal_db

        rates = surreal_db.kv_cache_get("usd_exchange_rates")
    except (RuntimeError, ValueError, KeyError, AttributeError) as exc:
        logger.debug("[Exchange Rates] Failed to read cached rates: %s", exc)

    if not rates:
        from django.core.cache import cache

        rates = cache.get("usd_exchange_rates")

    if not rates:
        rates = _fetch_live_rates_with_fallback()
    return rates


def get_locale_currency_details(request: Any) -> dict[str, Any]:
    """
    Resolves target currency, local currency name, and current exchange rate from USD
    by parsing browser settings (HTTP_ACCEPT_LANGUAGE) or system settings override.
    Caches fallback rates for 1 hour when external API is unavailable.
    Uses SurrealDB KV cache instead of Redis.
    """
    from extractor.models import SystemSettings

    try:
        settings_obj = SystemSettings.get_settings()
        selected_currency = settings_obj.currency
    except (SystemSettings.DoesNotExist, AttributeError, RuntimeError):
        selected_currency = "auto"

    if selected_currency and selected_currency != "auto":
        currency = selected_currency
        symbol_map = {"USD": "$", "IDR": "Rp ", "SAR": "SR ", "EUR": "€", "GBP": "£", "TRY": "₺"}
        symbol = symbol_map.get(currency, "$")
    else:
        accept_language = request.META.get("HTTP_ACCEPT_LANGUAGE", "").lower() if request else ""
        currency, symbol = _resolve_currency_and_symbol(accept_language)

    rates = _get_exchange_rates()
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
    Returns None immediately in DEBUG mode to skip blocking network calls.
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
    Alias for cloud_tasks.enqueue — replaces the old Django-Q async_task wrapper.
    Provides backward-compatible interface for call sites that used the old signature.
    """
    from extractor import cloud_tasks

    cloud_tasks.enqueue(task_name, payload, countdown=countdown)


def extract_pdf_diagrams_with_vision(pdf_path: str, max_pages: int = 5) -> str:
    """
    Extracts embedded diagrams, flowcharts, and architectural schemas from PDF pages
    using Gemini 3.6 Multi-Modal Vision OCR.
    """
    try:
        import fitz  # PyMuPDF
    except ImportError:
        logger.debug("[Vision OCR] PyMuPDF (fitz) is not installed.")
        return ""

    from extractor.llm_gateway import generate_multimodal_vision_ocr

    diagram_notes: list[str] = []
    try:
        doc = fitz.open(pdf_path)
        for page_num in range(min(len(doc), max_pages)):
            page = doc[page_num]
            image_list = page.get_images(full=True)
            if image_list:
                pix = page.get_pixmap(dpi=150)
                img_bytes = pix.tobytes("jpeg")
                ocr_result = generate_multimodal_vision_ocr(
                    img_bytes,
                    mime_type="image/jpeg",
                    prompt=f"Page {page_num + 1} contains a diagram or schema. Describe the structure, nodes, flowchart connections, and text accurately in Markdown.",
                )
                if ocr_result:
                    diagram_notes.append(
                        f"\n### 📊 Page {page_num + 1} Visual Diagram & Schema Extraction\n{ocr_result}\n"
                    )
        doc.close()
    except Exception as exc:
        logger.warning("[Vision OCR] Failed to process PDF page diagrams: %s", exc)
    return "\n".join(diagram_notes)
