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

import csv
import json
import logging
import sqlite3
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from typing import Any

from django.conf import settings
from django.utils.text import slugify

from extractor.file_markup import _build_yaml_frontmatter, _get_doc_field_str

logger = logging.getLogger(__name__)

APPLICATION_JSON = "application/json"
NO_COMPLETED_DOCS_MSG = "No completed documents selected for export bundle."
EXPORT_MAX_DOCUMENTS_LIMIT = 1000
EXPORT_MAX_LIMIT_MSG = f"Export batch exceeds maximum document limit of {EXPORT_MAX_DOCUMENTS_LIMIT} items."
# Cumulative uncompressed source-content budget. The document-count cap alone
# does not bound memory: the ZIP path stores each document ~4x (documents/,
# Language/, Author/, master_content) and the SQLite path holds every chunk plus
# its FTS index in RAM. 64 MiB of source text keeps worst-case amplification
# well under the 1Gi web-service memory floor.
EXPORT_MAX_TOTAL_CONTENT_BYTES = 64 * 1024 * 1024
EXPORT_TOO_LARGE_MSG = "Export exceeds the maximum total content size; select fewer documents."


@dataclass
class ZipExportContext:
    seen_lang_paths: set[str]
    seen_author_paths: set[str]
    manifest: dict[str, Any]
    master_content: list[str]
    zip_file: zipfile.ZipFile
    seen_doc_paths: set[str] = field(default_factory=set)
    include_taxonomic_views: bool = True


def _write_taxonomic_views(
    base_slug: str, clean_lang: str, clean_author: str, full_content: str, ctx: ZipExportContext
):
    """Helper to write duplicate organized views by language and author."""
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


def _append_master_source_block(idx: int, doc: Any, doc_type: str, doc_markdown: str, ctx: ZipExportContext):
    """Helper to append structured markdown source section to master document."""
    from html import escape

    clean_comment_title = escape(str(doc.title or "")).replace("--", "__")
    clean_comment_author = escape(str(doc.author or "")).replace("--", "__")
    clean_comment_lang = escape(str(doc.language or "")).replace("--", "__")
    clean_comment_type = escape(str(doc_type or "")).replace("--", "__")
    clean_comment_hash = escape(str(doc.file_hash or "")).replace("--", "__")

    ctx.master_content.append(
        f"<!-- SOURCE_START_{idx + 1}: {clean_comment_title} by {clean_comment_author} ({clean_comment_lang}) [Type: {clean_comment_type}, Hash: {clean_comment_hash}] -->"
    )
    ctx.master_content.append(f"\n# SOURCE: {doc.title}\n")
    ctx.master_content.append(f"**Author:** {doc.author}  ")
    ctx.master_content.append(f"**Language:** {doc.language}  ")
    ctx.master_content.append(f"**Document Type:** {doc_type}  ")
    if _get_doc_field_str(doc, "publisher"):
        ctx.master_content.append(f"**Publisher:** {_get_doc_field_str(doc, 'publisher')}  ")
    if _get_doc_field_str(doc, "publication_year"):
        ctx.master_content.append(f"**Publication Year:** {_get_doc_field_str(doc, 'publication_year')}  ")
    if _get_doc_field_str(doc, "doi"):
        ctx.master_content.append(f"**DOI:** {_get_doc_field_str(doc, 'doi')}  ")
    if _get_doc_field_str(doc, "license_type"):
        ctx.master_content.append(f"**License:** {_get_doc_field_str(doc, 'license_type')}  ")
    ctx.master_content.append("\n")
    ctx.master_content.append(doc_markdown)
    ctx.master_content.append(f"\n<!-- SOURCE_END_{idx + 1} -->\n\n---\n")


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

    # Primary single-copy standardized document output with collision deduplication
    doc_path = f"documents/{base_slug}.md"
    counter = 1
    while doc_path in ctx.seen_doc_paths:
        counter += 1
        doc_path = f"documents/{base_slug}_{counter}.md"
    ctx.seen_doc_paths.add(doc_path)
    ctx.zip_file.writestr(doc_path, full_content)

    if ctx.include_taxonomic_views:
        _write_taxonomic_views(base_slug, clean_lang, clean_author, full_content, ctx)

    ctx.manifest["documents"].append(
        {
            "id": doc.id,
            "filename": doc.original_filename,
            "title": doc.title,
            "author": doc.author,
            "language": doc.language,
            "type": doc_type,
            "publisher": _get_doc_field_str(doc, "publisher"),
            "publication_year": _get_doc_field_str(doc, "publication_year"),
            "doi": _get_doc_field_str(doc, "doi"),
            "license_type": _get_doc_field_str(doc, "license_type"),
            "page_count": int(getattr(doc, "page_count", 1) or 1),
            "cost_usd": float(getattr(doc, "cost_usd", 0.0) or 0.0),
            "hash": doc.file_hash,
            "export_path": doc_path,
        }
    )

    _append_master_source_block(idx, doc, doc_type, doc_markdown, ctx)


def _get_offline_docs(document_ids, user):
    import uuid

    from django.db.models import Q

    from extractor.models import SourceDocument

    int_ids = []
    uuid_strs = []
    for item in document_ids:
        try:
            int_ids.append(int(item))
        except ValueError, TypeError:
            try:
                uuid_strs.append(str(uuid.UUID(str(item))))
            except ValueError, TypeError, AttributeError:
                continue

    docs = SourceDocument.objects.filter(Q(id__in=int_ids) | Q(uuid__in=uuid_strs), status="COMPLETED")
    if user and not (user.is_staff or user.is_superuser):
        docs = docs.filter(uploaded_by=user)
    return list(docs)


def _surreal_doc_to_export(raw_doc, user, actor_id: str | None, users_map):
    from extractor.view_common import _wrap_surreal_doc

    if not raw_doc or raw_doc.get("status") != "COMPLETED":
        return None

    uploaded_by_id = raw_doc.get("uploaded_by_id")
    # Non-staff users can only export documents they own; unowned docs (uploaded_by_id is None) are excluded
    if user and not (user.is_staff or user.is_superuser):
        owner_id = actor_id or str(user.id)
        if not uploaded_by_id or uploaded_by_id != owner_id:
            return None

    return _wrap_surreal_doc(raw_doc, users_map)


def _get_surreal_docs(document_ids, user, actor_id: str | None = None):
    from extractor import surreal_db
    from extractor.view_common import _build_users_map

    docs_list = []
    raw_docs = surreal_db.get_documents(document_ids)
    user_ids = {d.get("uploaded_by_id") for d in raw_docs if d and d.get("uploaded_by_id")}
    users_map = _build_users_map(user_ids, fallback_user=user)

    for raw_doc in raw_docs:
        doc = _surreal_doc_to_export(raw_doc, user, actor_id, users_map)
        if doc is not None:
            docs_list.append(doc)
    return docs_list


def _doc_export_content_bytes(doc: Any) -> int:
    content = (
        getattr(doc, "refined_markdown", None)
        or getattr(doc, "cleaned_markdown", None)
        or getattr(doc, "raw_markdown", None)
        or ""
    )
    return len(str(content).encode("utf-8"))


def _enforce_export_doc_budget(docs_list: list) -> None:
    total = sum(_doc_export_content_bytes(d) for d in docs_list)
    if total > EXPORT_MAX_TOTAL_CONTENT_BYTES:
        raise ValueError(EXPORT_TOO_LARGE_MSG)


def generate_curated_zip_bundle(
    document_ids: list[int] | list[str],
    user: Any = None,
    include_taxonomic_views: bool = True,
    actor_id: str | None = None,
) -> bytes:
    """
    Aggregates selected documents into an organized directory structure.
    Saves unique copies under documents/ and optionally by Language/ and Author/.
    Prepends YAML frontmatter to each exported markdown file.
    Enforces user boundaries if `user` is provided and is not a staff/superuser.
    """

    if getattr(settings, "SURREALDB_OFFLINE", False):
        docs_list = _get_offline_docs(document_ids, user)
    else:
        docs_list = _get_surreal_docs(document_ids, user, actor_id=actor_id)

    if not docs_list:
        raise ValueError(NO_COMPLETED_DOCS_MSG)

    if len(docs_list) > EXPORT_MAX_DOCUMENTS_LIMIT:
        raise ValueError(EXPORT_MAX_LIMIT_MSG)

    _enforce_export_doc_budget(docs_list)

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
        "This master document contains all aggregated document knowledge in a single high-density layout. "
        "It uses strict structural indicators to help archival systems easily parse each separate source content boundaries.\n\n",
    ]

    seen_lang_paths: set[str] = set()
    seen_author_paths: set[str] = set()
    seen_doc_paths: set[str] = set()

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zip_file:
        ctx = ZipExportContext(
            seen_lang_paths=seen_lang_paths,
            seen_author_paths=seen_author_paths,
            seen_doc_paths=seen_doc_paths,
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


def _build_sft_pair(
    doc_info: tuple[str, str, str, str, str, str, str, str], chunk_item: dict[str, Any]
) -> dict[str, Any] | None:
    doc_uuid, title, author, language, publisher, publication_year, doi, license_type = doc_info
    chunk_content = str(chunk_item.get("content") or "")
    if not chunk_content.strip():
        return None
    page = chunk_item.get("page_number") or 1
    chapter = chunk_item.get("chapter_title") or ""
    anchor = chunk_item.get("anchor_id") or f"page-{page}"

    user_prompt = (
        f"Context from '{title}' by {author} (Page {page}"
        + (f", {chapter}" if chapter else "")
        + f"):\n\n{chunk_content}\n\n"
        "Question: Explain the key context, teachings, and significance of this excerpt."
    )
    assistant_response = f"Based on '{title}', the text conveys:\n\n{chunk_content}"

    return {
        "prompt": user_prompt,
        "completion": assistant_response,
        "messages": [
            {
                "role": "system",
                "content": "You are a knowledgeable archival research assistant specializing in historical, literary, and classical religious documents.",
            },
            {"role": "user", "content": user_prompt},
            {"role": "assistant", "content": assistant_response},
        ],
        "metadata": {
            "doc_uuid": doc_uuid,
            "title": title,
            "author": author,
            "language": language,
            "publisher": publisher,
            "publication_year": publication_year,
            "doi": doi,
            "license_type": license_type,
            "page_number": page,
            "chapter_title": chapter,
            "anchor_id": anchor,
        },
    }


def _get_doc_chunks(doc: Any, doc_uuid: str, limit: int) -> list[dict[str, Any]]:

    from extractor import surreal_db

    if getattr(settings, "SURREALDB_OFFLINE", False):
        from extractor.rag import chunk_document_semantically

        content = (
            getattr(doc, "refined_markdown", None)
            or getattr(doc, "cleaned_markdown", None)
            or getattr(doc, "raw_markdown", None)
            or ""
        )
        raw_chunks = chunk_document_semantically(content)
        from extractor.tasks import _build_chunk_payload

        lang = str(getattr(doc, "language", None) or "en")
        chunks_payload = []
        for idx, raw_text in enumerate(raw_chunks[:limit]):
            payload, _, _ = _build_chunk_payload(idx, raw_text, None, lang)
            chunks_payload.append(payload)
        return chunks_payload

    try:
        return surreal_db.get_document_chunks(doc_uuid, limit=limit)
    except Exception as e:
        logger.warning("[SFT Dataset] Failed to retrieve chunks for %s: %s", doc_uuid, e)
        return []


def _collect_pairs_for_document(doc, limit: int, current_count: int) -> list[dict[str, Any]]:
    """Helper to retrieve chunks and build SFT pairs for a single document."""
    doc_uuid = str(getattr(doc, "doc_uuid", None) or getattr(doc, "uuid", None) or doc.id)
    doc_info = (
        doc_uuid,
        getattr(doc, "title", None) or "Document",
        getattr(doc, "author", None) or "Author",
        getattr(doc, "language", None) or "en",
        _get_doc_field_str(doc, "publisher"),
        _get_doc_field_str(doc, "publication_year"),
        _get_doc_field_str(doc, "doi"),
        _get_doc_field_str(doc, "license_type"),
    )
    doc_pairs: list[dict[str, Any]] = []
    chunks = _get_doc_chunks(doc, doc_uuid, limit)
    for chunk_item in chunks[:limit]:
        pair = _build_sft_pair(doc_info, chunk_item)
        if pair:
            doc_pairs.append(pair)
        if current_count + len(doc_pairs) >= limit:
            break
    return doc_pairs


def generate_sft_dataset_pairs(
    document_ids: list[int] | list[str],
    user: Any = None,
    actor_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """
    Generates structured instruction fine-tuning (SFT) prompt-completion and chat message pairs
    from document chunks, respecting tenant isolation boundaries.
    """

    if getattr(settings, "SURREALDB_OFFLINE", False):
        docs_list = _get_offline_docs(document_ids, user)
    else:
        docs_list = _get_surreal_docs(document_ids, user, actor_id=actor_id)

    if not docs_list:
        raise ValueError(NO_COMPLETED_DOCS_MSG)

    if len(docs_list) > EXPORT_MAX_DOCUMENTS_LIMIT:
        raise ValueError(EXPORT_MAX_LIMIT_MSG)

    pairs: list[dict[str, Any]] = []
    total_pair_bytes = 0
    for doc in docs_list:
        doc_pairs = _collect_pairs_for_document(doc, limit, len(pairs))
        for pair in doc_pairs:
            total_pair_bytes += len(json.dumps(pair, ensure_ascii=False).encode("utf-8"))
            if total_pair_bytes > EXPORT_MAX_TOTAL_CONTENT_BYTES:
                raise ValueError(EXPORT_TOO_LARGE_MSG)
        pairs.extend(doc_pairs)
        if len(pairs) >= limit:
            break

    return pairs


def generate_sft_jsonl_bundle(
    document_ids: list[int] | list[str],
    user: Any = None,
    actor_id: str | None = None,
) -> bytes:
    """Generates standard Hugging Face JSONL formatted SFT dataset as UTF-8 bytes."""
    pairs = generate_sft_dataset_pairs(document_ids, user, actor_id=actor_id, limit=5000)
    lines = [json.dumps(p, ensure_ascii=False) for p in pairs]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _init_sqlite_export_schema(cursor: Any) -> None:
    cursor.execute("""
        CREATE TABLE documents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uuid TEXT UNIQUE,
            title TEXT,
            original_filename TEXT,
            author TEXT,
            language TEXT,
            publisher TEXT,
            publication_year TEXT,
            doi TEXT,
            license_type TEXT,
            file_hash TEXT,
            page_count INTEGER,
            cost_usd REAL,
            created_at TEXT
        );
    """)
    cursor.execute("""
        CREATE TABLE chunks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            doc_uuid TEXT,
            chunk_index INTEGER,
            page_number INTEGER,
            chapter_title TEXT,
            anchor_id TEXT,
            content TEXT,
            FOREIGN KEY (doc_uuid) REFERENCES documents(doc_uuid)
        );
    """)
    cursor.execute("""
        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            content,
            title,
            author,
            doc_uuid UNINDEXED,
            page_number UNINDEXED,
            anchor_id UNINDEXED,
            tokenize='porter unicode61'
        );
    """)


def _insert_sqlite_document(cursor: Any, doc: Any) -> str:
    doc_uuid = str(getattr(doc, "doc_uuid", None) or getattr(doc, "uuid", None) or doc.id)
    title = _get_doc_field_str(doc, "title", "Untitled")
    author = _get_doc_field_str(doc, "author", "Unknown")
    created_at_val = _get_doc_field_str(doc, "created_at", datetime.now(UTC).isoformat())

    cursor.execute(
        """
        INSERT INTO documents (
            doc_uuid, title, original_filename, author, language,
            publisher, publication_year, doi, license_type, file_hash,
            page_count, cost_usd, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
        """,
        (
            doc_uuid,
            title,
            _get_doc_field_str(doc, "original_filename"),
            author,
            _get_doc_field_str(doc, "language", "en"),
            _get_doc_field_str(doc, "publisher"),
            _get_doc_field_str(doc, "publication_year"),
            _get_doc_field_str(doc, "doi"),
            _get_doc_field_str(doc, "license_type"),
            _get_doc_field_str(doc, "file_hash"),
            int(getattr(doc, "page_count", 1) or 1),
            float(getattr(doc, "cost_usd", 0.0) or 0.0),
            created_at_val,
        ),
    )
    return doc_uuid


def _sqlite_chunk_rows(chunk: Any, doc_uuid: str, title: str, author: str) -> tuple[tuple, tuple] | None:
    content_str = str(chunk.get("content") or "").strip()
    if not content_str:
        return None
    p_num = int(chunk.get("page_number") or 1)
    anch = str(chunk.get("anchor_id") or f"page-{p_num}")
    c_idx = int(chunk.get("chunk_index") or 0)
    chunk_row = (doc_uuid, c_idx, p_num, str(chunk.get("chapter_title") or ""), anch, content_str)
    fts_row = (content_str, title, author, doc_uuid, str(p_num), anch)
    return chunk_row, fts_row


def _charge_export_byte_budget(byte_budget: dict[str, int], content_str: str) -> None:
    byte_budget["used"] += len(content_str.encode("utf-8"))
    if byte_budget["used"] > EXPORT_MAX_TOTAL_CONTENT_BYTES:
        raise ValueError(EXPORT_TOO_LARGE_MSG)


def _insert_sqlite_chunks(cursor: Any, doc: Any, doc_uuid: str, byte_budget: dict[str, int] | None = None) -> None:
    byte_budget = byte_budget if byte_budget is not None else {"used": 0}
    title = str(getattr(doc, "title", None) or "Untitled")
    author = str(getattr(doc, "author", None) or "Unknown")
    chunks = _get_doc_chunks(doc, doc_uuid, limit=500)
    chunk_rows: list[tuple] = []
    fts_rows: list[tuple] = []
    batch_size = 500
    insert_chunks_sql = """
        INSERT INTO chunks (
            doc_uuid, chunk_index, page_number, chapter_title, anchor_id, content
        ) VALUES (?, ?, ?, ?, ?, ?);
    """
    insert_fts_sql = """
        INSERT INTO chunks_fts (
            content, title, author, doc_uuid, page_number, anchor_id
        ) VALUES (?, ?, ?, ?, ?, ?);
    """

    def flush_batch() -> None:
        if not chunk_rows:
            return
        cursor.executemany(insert_chunks_sql, chunk_rows)
        cursor.executemany(insert_fts_sql, fts_rows)
        chunk_rows.clear()
        fts_rows.clear()

    for chunk in chunks:
        rows = _sqlite_chunk_rows(chunk, doc_uuid, title, author)
        if rows is None:
            continue
        _charge_export_byte_budget(byte_budget, rows[0][5])
        chunk_rows.append(rows[0])
        fts_rows.append(rows[1])
        if len(chunk_rows) == batch_size:
            flush_batch()
    flush_batch()


def _serialize_sqlite_conn(conn: Any) -> bytes:
    import os

    if hasattr(conn, "serialize"):
        raw_sqlite_bytes = conn.serialize()
        conn.close()
        return raw_sqlite_bytes

    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tmp_f:
            tmp_path = tmp_f.name

        file_conn = sqlite3.connect(tmp_path)
        try:
            conn.backup(file_conn)
        finally:
            file_conn.close()
            conn.close()

        with open(tmp_path, "rb") as f:
            raw_sqlite_bytes = f.read()
        return raw_sqlite_bytes
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as e:
                logger.debug("[FileUtils] Failed to remove temporary SQLite file %s: %s", tmp_path, e)


def generate_curated_sqlite_bundle(
    document_ids: list[int] | list[str],
    user: Any = None,
    actor_id: str | None = None,
) -> bytes:
    """
    Generates a standalone, self-contained SQLite database bundle with full-text search (FTS5).
    Includes 'documents', 'chunks', and virtual full-text search table 'chunks_fts'.
    Enforces user boundaries respecting tenant isolation.
    """

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        docs_list = _get_offline_docs(document_ids, user)
    else:
        docs_list = _get_surreal_docs(document_ids, user, actor_id=actor_id)

    if not docs_list:
        raise ValueError(NO_COMPLETED_DOCS_MSG)

    if len(docs_list) > EXPORT_MAX_DOCUMENTS_LIMIT:
        raise ValueError(EXPORT_MAX_LIMIT_MSG)

    conn = sqlite3.connect(":memory:")
    cursor = conn.cursor()
    _init_sqlite_export_schema(cursor)

    byte_budget = {"used": 0}
    for doc in docs_list:
        doc_uuid = _insert_sqlite_document(cursor, doc)
        _insert_sqlite_chunks(cursor, doc, doc_uuid, byte_budget)

    conn.commit()
    return _serialize_sqlite_conn(conn)


def _sanitize_csv_cell(val: Any) -> Any:
    if isinstance(val, str) and val.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "%", "|")):
        return f"'{val}"
    return val


def _extract_doc_excerpt(doc: Any) -> str:
    raw_content = (
        getattr(doc, "refined_markdown", None)
        or getattr(doc, "cleaned_markdown", None)
        or getattr(doc, "raw_markdown", None)
        or ""
    )
    excerpt = (raw_content[:500] + "...") if len(raw_content) > 500 else raw_content
    return " ".join(excerpt.split())


def _build_csv_row_for_doc(doc: Any) -> list[Any]:
    doc_uuid = str(getattr(doc, "doc_uuid", None) or getattr(doc, "uuid", None) or doc.id)
    raw_row = [
        doc_uuid,
        _get_doc_field_str(doc, "title", "Untitled"),
        _get_doc_field_str(doc, "author", "Unknown"),
        _get_doc_field_str(doc, "language", "en"),
        _get_doc_field_str(doc, "document_type", "PDF"),
        _get_doc_field_str(doc, "publisher"),
        _get_doc_field_str(doc, "publication_year"),
        _get_doc_field_str(doc, "doi"),
        _get_doc_field_str(doc, "license_type"),
        _get_doc_field_str(doc, "file_hash"),
        int(getattr(doc, "page_count", 1) or 1),
        float(getattr(doc, "cost_usd", 0.0) or 0.0),
        int(getattr(doc, "input_tokens", 0) or 0),
        int(getattr(doc, "output_tokens", 0) or 0),
        _get_doc_field_str(doc, "created_at"),
        _extract_doc_excerpt(doc),
    ]
    return [_sanitize_csv_cell(c) for c in raw_row]


def generate_curated_csv_bundle(
    document_ids: list[int] | list[str],
    user: Any = None,
    actor_id: str | None = None,
) -> bytes:
    """
    Generates a structured CSV export containing legal metadata, copyright info, token metrics,
    and excerpt content for all selected documents.
    """
    import io

    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        docs_list = _get_offline_docs(document_ids, user)
    else:
        docs_list = _get_surreal_docs(document_ids, user, actor_id=actor_id)

    if not docs_list:
        raise ValueError(NO_COMPLETED_DOCS_MSG)

    if len(docs_list) > EXPORT_MAX_DOCUMENTS_LIMIT:
        raise ValueError(EXPORT_MAX_LIMIT_MSG)

    output = io.StringIO()
    writer = csv.writer(output, quoting=csv.QUOTE_MINIMAL)

    writer.writerow(
        [
            "Document UUID",
            "Title",
            "Author",
            "Language",
            "Document Type",
            "Publisher",
            "Publication Year",
            "DOI",
            "License Type",
            "SHA-256 Hash",
            "Page Count",
            "AI Spend (USD)",
            "Input Tokens",
            "Output Tokens",
            "Created At",
            "Content Excerpt",
        ]
    )

    import codecs

    for doc in docs_list:
        writer.writerow(_build_csv_row_for_doc(doc))

    return codecs.BOM_UTF8 + output.getvalue().encode("utf-8")
