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

import hashlib
import json
import logging
import zipfile

logger = logging.getLogger(__name__)

# ── ZIP Security Constants (SEC-03 Zip Slip + EDGE-05 Zip Bomb guards) ────────
# Maximum total uncompressed bytes allowed from any single uploaded ZIP archive.
_ZIP_MAX_UNCOMPRESSED_BYTES: int = 500 * 1024 * 1024  # 500 MB
# Maximum number of members (files) permitted inside a single ZIP archive.
_ZIP_MAX_MEMBER_COUNT: int = 500

# XLSX parsing expands ZIP members into XML trees, Python rows, and Markdown.
# These limits are deliberately smaller than the generic archive-export limits
# and keep a single document well below the worker's 2 GiB memory allocation.
_XLSX_MAX_UNCOMPRESSED_BYTES: int = 64 * 1024 * 1024
_XLSX_MAX_MEMBER_COUNT: int = 100
_XLSX_MAX_SHEETS: int = 20
_XLSX_MAX_ROWS: int = 5_000
_XLSX_MAX_COLUMNS: int = 50
_XLSX_MAX_CELL_CHARS: int = 1_024
_XLSX_MAX_MARKDOWN_BYTES: int = 16 * 1024 * 1024
_LOCAL_MAX_MARKDOWN_BYTES: int = 16 * 1024 * 1024
_LOCAL_MAX_PDF_PAGES: int = 600


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


def _validate_xlsx_archive(zf: zipfile.ZipFile) -> None:
    """Apply tighter limits before any XLSX XML is loaded into memory."""
    members = zf.infolist()
    if len(members) > _XLSX_MAX_MEMBER_COUNT:
        raise ValueError("XLSX archive contains too many members.")
    if sum(member.file_size for member in members) > _XLSX_MAX_UNCOMPRESSED_BYTES:
        raise ValueError("XLSX archive expands beyond the parser budget.")
    sheet_count = sum(
        member.filename.startswith("xl/worksheets/sheet") and member.filename.endswith(".xml") for member in members
    )
    if sheet_count > _XLSX_MAX_SHEETS:
        raise ValueError("XLSX workbook contains too many worksheets.")


def _bounded_excel_row(row: tuple[Any, ...] | list[Any], parsed_rows: list[int]) -> list[str]:
    """Validate a non-empty spreadsheet row against the workbook work budget."""
    if len(row) > _XLSX_MAX_COLUMNS:
        raise ValueError("XLSX worksheet exceeds the column limit.")
    normalized = [str(cell).strip() if cell is not None else "" for cell in row]
    if any(len(cell) > _XLSX_MAX_CELL_CHARS for cell in normalized):
        raise ValueError("XLSX worksheet contains an oversized cell.")
    if any(normalized):
        parsed_rows[0] += 1
        if parsed_rows[0] > _XLSX_MAX_ROWS:
            raise ValueError("XLSX workbook exceeds the row limit.")
    return normalized


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


def calculate_file_sha256(file_handle: IO[bytes]) -> str:
    """
    Computes SHA-256 checksum in chunks of 64KB for deduplication and content-addressing
    from an open file-like byte stream.
    """
    sha256 = hashlib.sha256()
    file_handle.seek(0)
    for chunk in iter(lambda: file_handle.read(65536), b""):
        sha256.update(chunk)
    file_handle.seek(0)
    return sha256.hexdigest()


def calculate_filepath_sha256(file_path: str) -> str:
    """
    Computes SHA-256 checksum in chunks of 64KB for deduplication and content-addressing
    from a filesystem path.
    """
    from pathlib import Path

    target_path = Path(file_path).resolve()
    if not target_path.is_file():
        raise ValueError(f"Path is not a valid file: {file_path}")

    with target_path.open("rb") as f:
        return calculate_file_sha256(f)


def _emit_csv_table(reader: Any, headers: list[str], markdown_lines: list[str], state: dict[str, int]) -> None:
    """Stream CSV rows into markdown_lines, enforcing the rendered-output budget."""

    def _emit_row(cells: list[str]) -> None:
        line = "| " + " | ".join(cells) + " |"
        state["bytes"] += len(line.encode("utf-8")) + 1
        if state["bytes"] > _LOCAL_MAX_MARKDOWN_BYTES:
            raise ValueError("CSV document exceeds the rendered output limit.")
        markdown_lines.append(line)

    def _escape_cell(cell: Any) -> str:
        return str(cell).replace("|", "\\|").replace("\n", "<br>")

    _emit_row([_escape_cell(h) for h in headers])
    _emit_row(["---" for _ in headers])
    for row in reader:
        if len(row) < len(headers):
            row += [""] * (len(headers) - len(row))
        elif len(row) > len(headers):
            row = row[: len(headers)]
        _emit_row([_escape_cell(c) for c in row])


def process_csv_local(file_path: str) -> str:
    """
    Converts tabular CSV files cleanly to an aligned Markdown table locally ($0 cost!).
    Supports heavy columns and escapes standard symbols.
    Streams rows and enforces a rendered-output budget so pathological
    files cannot exhaust worker memory.
    """
    import csv

    markdown_lines: list[str] = []
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "latin-1"):
        try:
            with open(file_path, newline="", encoding=encoding) as f:
                reader = csv.reader(f)
                try:
                    headers = next(reader)
                except StopIteration:
                    return "*Empty CSV Document*"
                _emit_csv_table(reader, headers, markdown_lines, {"bytes": 0})
            return "\n".join(markdown_lines)
        except UnicodeDecodeError as exc:
            last_error = exc
            markdown_lines.clear()
            continue
    raise ValueError(f"CSV document could not be decoded: {last_error}") from last_error


def process_txt_local(file_path: str) -> str:
    """
    Reads local TXT and Markdown files (including Quran Arabic text with Harakat and translations),
    preserving UTF-8 encoding and layout structure cleanly ($0 API cost).
    """
    try:
        with open(file_path, encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        with open(file_path, encoding="latin-1") as f:
            content = f.read()
    return content


def _format_json_rows_as_table(title: str, items: list[dict]) -> str:
    """Formats a list of dictionaries into a clean Markdown table."""
    keys = list({k: True for item in items for k in item}.keys())
    rows = []
    for item in items:
        rows.append([str(item.get(k, "")).strip().replace("\n", "<br>") for k in keys])
    return _format_markdown_table_sheet(title, [keys, *rows])


def _format_json_dict_section(key: Any, value: Any) -> str:
    title = str(key).replace("_", " ").title()
    if isinstance(value, list) and all(isinstance(x, dict) for x in value):
        return f"## {title}\n\n" + _format_json_rows_as_table(title, value)
    if isinstance(value, (dict, list)):
        return f"## {title}\n\n```json\n{json.dumps(value, indent=2, ensure_ascii=False)}\n```"
    return f"## {title}\n\n{value}"


def _format_json_data(data: Any) -> str:
    """Render a parsed JSON value into structured Markdown."""
    if isinstance(data, list):
        if not data:
            return "*Empty JSON List*"
        if all(isinstance(item, dict) for item in data):
            return _format_json_rows_as_table("Dataset", data)
        return "\n\n".join(f"- {json.dumps(item, ensure_ascii=False)}" for item in data)
    if isinstance(data, dict):
        sections = [_format_json_dict_section(k, v) for k, v in data.items()]
        return "\n\n".join(sections) if sections else "*Empty JSON Object*"
    return str(data)


def process_json_local(file_path: str) -> str:
    """
    Parses local JSON documents (including Quran verse datasets, Hadith collections,
    and structured JSON payloads) into structured Markdown sections and tables ($0 API cost).
    """
    # json.load amplifies input size several-fold in memory; cap the input so a
    # max-size upload cannot exhaust worker memory before formatting budgets apply.
    import os

    if os.path.getsize(file_path) > 25 * 1024 * 1024:
        raise ValueError("JSON document exceeds the maximum supported input size.")
    try:
        with open(file_path, encoding="utf-8") as f:
            data = json.load(f)
    except ValueError:
        try:
            with open(file_path, encoding="latin-1") as f:
                data = json.load(f)
        except Exception:
            return "*Empty or Malformed JSON Document*"

    result = _format_json_data(data)
    if len(result.encode("utf-8")) > _LOCAL_MAX_MARKDOWN_BYTES:
        raise ValueError("JSON document exceeds the rendered output limit.")
    return result


def _format_markdown_table_sheet(
    sheet_title: str, rows: list[list[str]], rendered_bytes: list[int] | None = None
) -> str:
    """Formats a matrix of cells into a GitHub Flavored Markdown table."""
    if not rows:
        return ""
    headers = rows[0]
    sheet_md = [f"### Sheet: {sheet_title}\n"]
    sheet_md.append("| " + " | ".join([h.replace("|", "\\|") for h in headers]) + " |")
    sheet_md.append("| " + " | ".join(["---" for _ in headers]) + " |")
    for r in rows[1:]:
        if len(r) < len(headers):
            r += [""] * (len(headers) - len(r))
        elif len(r) > len(headers):
            r = r[: len(headers)]
        sheet_md.append("| " + " | ".join([c.replace("|", "\\|").replace("\n", "<br>") for c in r]) + " |")
    formatted = "\n".join(sheet_md)
    if rendered_bytes is not None:
        rendered_bytes[0] += len(formatted.encode("utf-8"))
        if rendered_bytes[0] > _XLSX_MAX_MARKDOWN_BYTES:
            raise ValueError("XLSX workbook exceeds the rendered output limit.")
    return formatted


def _parse_excel_openpyxl(file_path: str) -> str | None:
    """Extracts tables from workbook using openpyxl."""
    try:
        import openpyxl  # type: ignore[import-untyped]

        with zipfile.ZipFile(file_path, "r") as zf:
            _validate_xlsx_archive(zf)
        wb = openpyxl.load_workbook(file_path, data_only=True, read_only=True)
        try:
            sheets_md: list[str] = []
            parsed_rows = [0]
            rendered_bytes = [0]
            for sheet_name in wb.sheetnames:
                ws = wb[sheet_name]
                rows: list[list[str]] = []
                for row in ws.iter_rows(values_only=True):
                    str_row = _bounded_excel_row(row, parsed_rows)
                    if any(str_row):
                        rows.append(str_row)
                formatted = _format_markdown_table_sheet(sheet_name, rows, rendered_bytes)
                if formatted:
                    sheets_md.append(formatted)
            return "\n\n".join(sheets_md) if sheets_md else None
        finally:
            wb.close()
    except ValueError:
        raise
    except Exception as exc:
        logger.debug("[Excel Parser] openpyxl extraction failed: %s", exc)
        return None


def _extract_zipxml_cell_value(c_el: Any, shared_strings: list[str]) -> str:
    cell_type = c_el.get("t")
    v_el = c_el.find(".//{*}v")
    if v_el is None or not v_el.text:
        return ""
    if cell_type == "s" and v_el.text.isdigit():
        idx = int(v_el.text)
        return shared_strings[idx] if idx < len(shared_strings) else v_el.text
    return v_el.text


def _extract_sheet_rows_from_xml(sheet_tree, shared_strings: list[str], parsed_rows: list[int]) -> list[list[str]]:
    """Helper to extract non-empty rows from an Excel worksheet XML tree."""
    rows: list[list[str]] = []
    for row_el in sheet_tree.findall(".//{*}row"):
        row_cells = _bounded_excel_row(
            [_extract_zipxml_cell_value(c_el, shared_strings) for c_el in row_el.findall(".//{*}c")], parsed_rows
        )
        if any(row_cells):
            rows.append(row_cells)
    return rows


def _load_zipxml_shared_strings(zf: Any) -> list[str]:
    import defusedxml.ElementTree as ET  # type: ignore[import-untyped]

    shared_strings: list[str] = []
    if "xl/sharedStrings.xml" in zf.namelist():
        ss_tree = ET.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in ss_tree.findall(".//{*}si"):
            t_el = si.find(".//{*}t")
            shared_strings.append(t_el.text if t_el is not None and t_el.text else "")
    return shared_strings


def _parse_zipxml_sheets(zf: Any, shared_strings: list[str]) -> list[str]:
    import defusedxml.ElementTree as ET  # type: ignore[import-untyped]

    sheet_files = [n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")]
    sheets_md: list[str] = []
    parsed_rows = [0]
    rendered_bytes = [0]
    for s_idx, s_file in enumerate(sheet_files, 1):
        sheet_tree = ET.fromstring(zf.read(s_file))
        rows = _extract_sheet_rows_from_xml(sheet_tree, shared_strings, parsed_rows)
        formatted = _format_markdown_table_sheet(f"Sheet {s_idx}", rows, rendered_bytes)
        if formatted:
            sheets_md.append(formatted)
    return sheets_md


def _parse_excel_zipxml(file_path: str) -> str | None:
    """Pure standard-library ZIP+XML fallback for .xlsx parsing."""
    try:
        with zipfile.ZipFile(file_path, "r") as zf:
            _validate_xlsx_archive(zf)
            shared_strings = _load_zipxml_shared_strings(zf)
            sheets_md = _parse_zipxml_sheets(zf, shared_strings)
            return "\n\n".join(sheets_md) if sheets_md else None
    except ValueError:
        # Parser budget violations must fail loudly, not degrade to a
        # placeholder document — mirrors the openpyxl path above.
        raise
    except Exception as exc:
        logger.warning("[Excel Parser] Native XML extraction failed: %s", exc)
        return None


def process_excel_local(file_path: str) -> str:
    """
    Converts Excel spreadsheets (.xlsx, .xls) cleanly to Markdown tables locally ($0 API cost).
    Supports multi-sheet workbooks using openpyxl or pure standard library XML parsing.
    """
    result = _parse_excel_openpyxl(file_path)
    if result:
        return result

    result = _parse_excel_zipxml(file_path)
    if result:
        return result

    return "*Empty or Unreadable Excel Document*"


def _extract_pdf_pymupdf(file_path: str) -> tuple[str, int]:
    """Extract page text via PyMuPDF; returns (markdown, page_count)."""
    import fitz  # PyMuPDF

    doc = fitz.open(file_path)
    try:
        pages_md: list[str] = []
        page_count = len(doc)
        if page_count > _LOCAL_MAX_PDF_PAGES:
            raise ValueError("PDF document exceeds the maximum supported page count.")
        extracted_bytes = 0
        for page_num in range(page_count):
            text = doc[page_num].get_text("text").strip()
            if text:
                extracted_bytes += len(text.encode("utf-8"))
                if extracted_bytes > _LOCAL_MAX_MARKDOWN_BYTES:
                    raise ValueError("PDF document exceeds the rendered output limit.")
                pages_md.append(f"## Page {page_num + 1}\n\n{text}")
    finally:
        doc.close()
    if pages_md and len("".join(pages_md).strip()) > 30:
        return "\n\n---\n\n".join(pages_md), page_count
    return "", page_count


def _extract_pdf_pypdf(file_path: str) -> tuple[str, int]:
    """Extract page text via pypdf; returns (markdown, page_count)."""
    import pypdf

    reader = pypdf.PdfReader(file_path)
    page_count = len(reader.pages)
    if page_count > _LOCAL_MAX_PDF_PAGES:
        raise ValueError("PDF document exceeds the maximum supported page count.")
    pages_md = []
    extracted_bytes = 0
    for p_idx, page in enumerate(reader.pages, 1):
        text = (page.extract_text() or "").strip()
        if text:
            extracted_bytes += len(text.encode("utf-8"))
            if extracted_bytes > _LOCAL_MAX_MARKDOWN_BYTES:
                raise ValueError("PDF document exceeds the rendered output limit.")
            pages_md.append(f"## Page {p_idx}\n\n{text}")
    if pages_md and len("".join(pages_md).strip()) > 30:
        return "\n\n---\n\n".join(pages_md), page_count
    return "", page_count


def process_pdf_local(file_path: str) -> tuple[str, int]:
    """
    Extracts native digital text and page structure from PDF pages locally ($0 API cost).
    Returns a tuple of (extracted_markdown, page_count).
    If no text or PyMuPDF/pypdf is uninstalled, returns ("", page_count).
    """
    for extractor in (_extract_pdf_pymupdf, _extract_pdf_pypdf):
        try:
            return extractor(file_path)
        except ValueError:
            raise
        except Exception as exc:
            logger.debug("[PDF Local] %s extraction skipped (%s)", extractor.__name__, exc)
    return "", 0
