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

import ipaddress
import json
import logging
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any

from django.conf import settings
from django.utils.text import slugify

from extractor.export_generators import (
    APPLICATION_JSON,
    EXPORT_MAX_DOCUMENTS_LIMIT,
    EXPORT_MAX_LIMIT_MSG,
    EXPORT_MAX_TOTAL_CONTENT_BYTES,
    EXPORT_TOO_LARGE_MSG,
    NO_COMPLETED_DOCS_MSG,
    ZipExportContext,
    _append_master_source_block,
    _build_csv_row_for_doc,
    _build_sft_pair,
    _charge_export_byte_budget,
    _collect_pairs_for_document,
    _doc_export_content_bytes,
    _enforce_export_doc_budget,
    _extract_doc_excerpt,
    _get_doc_chunks,
    _get_offline_docs,
    _get_surreal_docs,
    _init_sqlite_export_schema,
    _insert_sqlite_chunks,
    _insert_sqlite_document,
    _process_zip_doc,
    _sanitize_csv_cell,
    _serialize_sqlite_conn,
    _sqlite_chunk_rows,
    _surreal_doc_to_export,
    _write_taxonomic_views,
    generate_curated_csv_bundle,
    generate_curated_sqlite_bundle,
    generate_curated_zip_bundle,
    generate_sft_dataset_pairs,
    generate_sft_jsonl_bundle,
)
from extractor.file_ingest import (
    _LOCAL_MAX_MARKDOWN_BYTES,
    _LOCAL_MAX_PDF_PAGES,
    _XLSX_MAX_CELL_CHARS,
    _XLSX_MAX_COLUMNS,
    _XLSX_MAX_MARKDOWN_BYTES,
    _XLSX_MAX_MEMBER_COUNT,
    _XLSX_MAX_ROWS,
    _XLSX_MAX_SHEETS,
    _XLSX_MAX_UNCOMPRESSED_BYTES,
    _ZIP_MAX_MEMBER_COUNT,
    _ZIP_MAX_UNCOMPRESSED_BYTES,
    _bounded_excel_row,
    _emit_csv_table,
    _extract_pdf_pymupdf,
    _extract_pdf_pypdf,
    _extract_sheet_rows_from_xml,
    _extract_zipxml_cell_value,
    _format_json_data,
    _format_json_dict_section,
    _format_json_rows_as_table,
    _format_markdown_table_sheet,
    _load_zipxml_shared_strings,
    _parse_excel_openpyxl,
    _parse_excel_zipxml,
    _parse_zipxml_sheets,
    _validate_xlsx_archive,
    calculate_file_sha256,
    calculate_filepath_sha256,
    process_csv_local,
    process_excel_local,
    process_json_local,
    process_pdf_local,
    process_txt_local,
    safe_extract,
    validate_zip,
)
from extractor.file_markup import (
    _BLEACH_ALLOWED_ATTRS,
    _BLEACH_ALLOWED_TAGS,
    _BLEACH_CLEANER,
    _add_noopener_rel,
    _apply_arabic_attributes,
    _build_yaml_frontmatter,
    _detect_first_strong,
    _escape_yaml_val,
    _get_doc_field_str,
    clean_html_content,
    parse_arabic_layout,
    render_markdown_to_html,
)

logger = logging.getLogger(__name__)


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


# ── Fallback exchange rates (used if the external API is offline) ─────────────
# Approximate rates as of mid-2026 — stale but far better than a blocking 5s timeout.
_FALLBACK_RATES: dict[str, float] = {
    "USD": 1.0,
    "EUR": 0.92,
    "GBP": 0.79,
    "IDR": 16200.0,
    "SAR": 3.75,
    "TRY": 32.0,
    "BRL": 5.5,
    "MXN": 17.0,
    "ARS": 1000.0,
    "CLP": 950.0,
    "COP": 4000.0,
    "PEN": 3.7,
    "AUD": 1.5,
    "CAD": 1.4,
    "INR": 83.0,
    "JPY": 150.0,
    "KRW": 1350.0,
    "SGD": 1.35,
}

# Currencies whose minor unit is not used in everyday pricing.
_ZERO_DECIMAL_CURRENCIES = {"IDR", "JPY", "KRW"}

_CURRENCY_SYMBOLS: dict[str, str] = {
    "USD": "$",
    "EUR": "€",
    "GBP": "£",
    "IDR": "Rp ",
    "SAR": "SR ",
    "TRY": "₺",
    "BRL": "R$ ",
    "MXN": "$",
    "ARS": "$",
    "CLP": "$",
    "COP": "$",
    "PEN": "S/ ",
    "AUD": "A$",
    "CAD": "C$",
    "INR": "₹",
    "JPY": "¥",
    "KRW": "₩",
    "SGD": "S$",
}

# Region subtags whose currency differs from the language default —
# e.g. "es-MX" must not resolve to EUR, "pt-BR" must not resolve to EUR.
_REGION_CURRENCY: dict[str, tuple[str, str]] = {
    "br": ("BRL", "R$ "),
    "mx": ("MXN", "$"),
    "ar": ("ARS", "$"),
    "cl": ("CLP", "$"),
    "co": ("COP", "$"),
    "pe": ("PEN", "S/ "),
    "au": ("AUD", "A$"),
    "ca": ("CAD", "C$"),
    "in": ("INR", "₹"),
    "jp": ("JPY", "¥"),
    "kr": ("KRW", "₩"),
    "sg": ("SGD", "S$"),
}


# Thread-safe in-memory cache for exchange rates (1-hour TTL)
_rates_cache: dict[str, Any] = {}
_rates_cache_lock = threading.Lock()


def cleanup_stale_temp_artifacts(temp_dir: str | None = None, max_age_seconds: int = 86400) -> int:
    """Automated cleanup policy per DevSecOps best practices.

    Scans temporary directories for stale processing files older than max_age_seconds (default: 24h).
    Returns total count of removed temporary artifacts.
    """
    import os

    # VULN-01 / S5443 fix: resolve absolute realpath to prevent symlink traversal in temp dir
    raw_dir = (
        temp_dir if temp_dir is not None else tempfile.gettempdir()
    )  # NOSONAR python:S5443 -- Resolved with realpath validation before use.
    target_dir = os.path.realpath(raw_dir)
    if not os.path.exists(target_dir):
        return 0

    now = time.time()
    removed_count = 0
    prefix_patterns = ("tmpx", "tmp", "korda_", "aetheromni_", "pdf_export_")

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
        except OSError as exc:
            logger.debug("[File Cleanup] Could not remove temporary file %s: %s", file_path, exc)

    return removed_count


def _is_internal_proxy_addr(addr: Any) -> bool:
    if addr.is_loopback or addr.is_link_local:
        return True
    packed = addr.packed
    if addr.version == 4:
        return (
            packed[0] == 10 or (packed[0] == 172 and 16 <= packed[1] <= 31) or (packed[0] == 192 and packed[1] == 168)
        )
    return (packed[0] & 0xFE) == 0xFC


def _is_trusted_proxy_ip(ip_str: str) -> bool:

    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return _is_internal_proxy_addr(addr)


def _parse_valid_ip(raw_token: str) -> tuple[str, bool] | None:

    candidate = raw_token.strip()
    if not candidate:
        return None
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return str(addr), _is_internal_proxy_addr(addr)


def get_client_ip(request: Any) -> str:
    """Extract the true client IP, ignoring spoofed X-Forwarded-For from untrusted peers."""
    remote_addr = str(request.META.get("REMOTE_ADDR", "127.0.0.1")).strip() or "127.0.0.1"
    x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
    if not x_forwarded_for or not _is_trusted_proxy_ip(remote_addr):
        return remote_addr

    parsed = [res for part in str(x_forwarded_for).split(",") if (res := _parse_valid_ip(part)) is not None]
    if not parsed:
        return remote_addr

    for ip_str, is_internal in reversed(parsed):
        if not is_internal:
            return ip_str
    return parsed[-1][0]


def build_gcs_prefix_path(user_id: str, doc_slug: str, filename: str, *, ephemeral: bool = True) -> str:
    """Build deterministic GCS prefix key under raw/ephemeral/ or raw/latest/."""
    import os
    import uuid

    safe_user = slugify(str(user_id)) or "anon"
    safe_slug = slugify(str(doc_slug)) or "document"
    base, ext = os.path.splitext(str(filename or "upload.bin"))
    safe_name = f"{slugify(base) or 'file'}{ext.lower()}"
    if ephemeral:
        return f"raw/ephemeral/{safe_user}/{safe_slug}/{uuid.uuid4().hex[:12]}-{safe_name}"
    return f"raw/latest/{safe_user}/{safe_slug}/{safe_name}"


def _resolve_currency_and_symbol(accept_language: str) -> tuple[str, str]:
    import re

    # Parse the highest-weighted language tag: naive substring checks misfire on
    # region subtags (e.g. "es-AR" contains "ar" and would wrongly pick SAR).
    first_tag = (accept_language or "").split(",")[0].split(";")[0].strip().lower()
    parts = [p for p in re.split(r"[-_]", first_tag) if p]
    lang = parts[0] if parts else ""
    region = parts[1] if len(parts) > 1 else ""

    # Region wins over language defaults when the locale pins a distinct
    # currency (es-MX, pt-BR, en-AU, ...); otherwise fall back to language.
    region_currency = _REGION_CURRENCY.get(region)
    if region_currency is not None:
        return region_currency

    if lang == "id":
        return "IDR", "Rp "
    if lang == "ar":
        return "SAR", "SR "
    if lang in {"de", "fr", "es", "it", "nl", "pt"}:
        return "EUR", "€"
    if region == "gb":
        return "GBP", "£"
    if lang == "tr":
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
        logger.exception("[Exchange Rates] Error fetching live rates: %s — using fallback.", exc)
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
        try:
            from django.core.cache import cache

            cache.set("usd_exchange_rates", rates, 3600)
        except Exception as exc:
            logger.debug("[Exchange Rates] Failed to populate local cache: %s", exc)
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
    except SystemSettings.DoesNotExist, AttributeError, RuntimeError:
        selected_currency = "auto"

    if selected_currency and selected_currency != "auto":
        currency = selected_currency
        symbol = _CURRENCY_SYMBOLS.get(currency, "$")
    else:
        accept_language = request.META.get("HTTP_ACCEPT_LANGUAGE", "").lower() if request else ""
        currency, symbol = _resolve_currency_and_symbol(accept_language)

    rates = _get_exchange_rates()
    rate = rates.get(currency, 1.0) if rates else 1.0
    # Exchange-rate API values are untrusted: keep only finite positive
    # numerics so a malformed upstream payload can't crash rendering or
    # display negative localized prices (AUD-381).
    try:
        rate_dec = Decimal(str(rate))
        if not rate_dec.is_finite() or rate_dec <= 0:
            rate_dec = Decimal("1")
    except Exception:
        rate_dec = Decimal("1")
    return {"currency_code": currency, "symbol": symbol, "rate": rate_dec}


def format_localized_cost(cost_usd: Decimal, currency_details: dict[str, Any]) -> str:
    """Formats USD pricing into both localized and USD formats."""
    usd_val = Decimal(str(cost_usd))
    try:
        rate = Decimal(str(currency_details["rate"]))
        if not rate.is_finite() or rate <= 0:
            rate = Decimal("1")
    except Exception:
        rate = Decimal("1")
    local_val = usd_val * rate

    def fmt_usd(v: Decimal) -> str:
        if v < Decimal("0.10") and v > 0:
            return f"${v:.4f}"
        return f"${v:.2f}"

    if currency_details["currency_code"] == "USD":
        return fmt_usd(usd_val)

    if currency_details["currency_code"] in _ZERO_DECIMAL_CURRENCIES:
        return f"{currency_details['symbol']}{int(local_val):,} (~{fmt_usd(usd_val)})"

    return f"{currency_details['symbol']}{local_val:.2f} (~{fmt_usd(usd_val)})"


def get_google_oidc_token(audience: str) -> str | None:
    """
    Fetches an OIDC ID token from the GCP metadata server for the given audience.
    Returns None immediately in DEBUG mode to skip blocking network calls.
    """
    if settings.DEBUG:
        return None

    query = urllib.parse.urlencode({"audience": audience})
    url = f"http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/identity?{query}"
    req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:  # nosec B310 nosemgrep
            return response.read().decode("utf-8").strip()
    except Exception as exc:
        logger.warning("[OIDC] Failed to fetch OIDC identity (not in GCP?): %s", exc)
        return None


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
        with fitz.open(pdf_path) as doc:
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
    except Exception as exc:
        logger.warning("[Vision OCR] Failed to process PDF page diagrams: %s", exc)
    return "\n".join(diagram_notes)


__all__ = [
    "APPLICATION_JSON",
    "EXPORT_MAX_DOCUMENTS_LIMIT",
    "EXPORT_MAX_LIMIT_MSG",
    "EXPORT_MAX_TOTAL_CONTENT_BYTES",
    "EXPORT_TOO_LARGE_MSG",
    "NO_COMPLETED_DOCS_MSG",
    "_BLEACH_ALLOWED_ATTRS",
    "_BLEACH_ALLOWED_TAGS",
    "_BLEACH_CLEANER",
    "_LOCAL_MAX_MARKDOWN_BYTES",
    "_LOCAL_MAX_PDF_PAGES",
    "_XLSX_MAX_CELL_CHARS",
    "_XLSX_MAX_COLUMNS",
    "_XLSX_MAX_MARKDOWN_BYTES",
    "_XLSX_MAX_MEMBER_COUNT",
    "_XLSX_MAX_ROWS",
    "_XLSX_MAX_SHEETS",
    "_XLSX_MAX_UNCOMPRESSED_BYTES",
    "_ZIP_MAX_MEMBER_COUNT",
    "_ZIP_MAX_UNCOMPRESSED_BYTES",
    "ZipExportContext",
    "_add_noopener_rel",
    "_append_master_source_block",
    "_apply_arabic_attributes",
    "_bounded_excel_row",
    "_build_csv_row_for_doc",
    "_build_sft_pair",
    "_build_yaml_frontmatter",
    "_charge_export_byte_budget",
    "_collect_pairs_for_document",
    "_detect_first_strong",
    "_doc_export_content_bytes",
    "_emit_csv_table",
    "_enforce_export_doc_budget",
    "_escape_yaml_val",
    "_extract_doc_excerpt",
    "_extract_pdf_pymupdf",
    "_extract_pdf_pypdf",
    "_extract_sheet_rows_from_xml",
    "_extract_zipxml_cell_value",
    "_format_json_data",
    "_format_json_dict_section",
    "_format_json_rows_as_table",
    "_format_markdown_table_sheet",
    "_get_doc_chunks",
    "_get_doc_field_str",
    "_get_offline_docs",
    "_get_surreal_docs",
    "_init_sqlite_export_schema",
    "_insert_sqlite_chunks",
    "_insert_sqlite_document",
    "_load_zipxml_shared_strings",
    "_parse_excel_openpyxl",
    "_parse_excel_zipxml",
    "_parse_zipxml_sheets",
    "_process_zip_doc",
    "_sanitize_csv_cell",
    "_serialize_sqlite_conn",
    "_sqlite_chunk_rows",
    "_surreal_doc_to_export",
    "_validate_xlsx_archive",
    "_write_taxonomic_views",
    "calculate_file_sha256",
    "calculate_filepath_sha256",
    "clean_html_content",
    "generate_curated_csv_bundle",
    "generate_curated_sqlite_bundle",
    "generate_curated_zip_bundle",
    "generate_sft_dataset_pairs",
    "generate_sft_jsonl_bundle",
    "parse_arabic_layout",
    "process_csv_local",
    "process_excel_local",
    "process_json_local",
    "process_pdf_local",
    "process_txt_local",
    "render_markdown_to_html",
    "safe_extract",
    "validate_zip",
]
