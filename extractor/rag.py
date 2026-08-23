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

"""
Retrieval-Augmented Generation (RAG) & Semantic Search Engine — AetherOmni v2.0

Provides:
  - Semantic document chunking tailored for multilingual text (Latin, Arabic)
  - HNSW 768 cosine vector similarity search via SurrealDB
  - Dynamic reranking and context-aware citation synthesis
  - Semantic query caching and token-budget aware context assembly
"""

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from extractor.llm_gateway import generate_llm_content_unified

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ERROR = "GEMINI_API_KEY is not configured."


def _ingest_paragraph_into_chunks(
    p: str, max_chunk_size: int, current_chunk: list[str], current_size: int, chunks: list[str]
) -> tuple[list[str], int]:
    """Helper to append a paragraph to the active chunk or split long paragraphs."""
    p_len = len(p)
    if current_size + p_len <= max_chunk_size:
        current_chunk.append(p)
        return current_chunk, current_size + p_len + 2

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    if p_len > max_chunk_size:
        return _chunk_long_paragraph(p, max_chunk_size, chunks)

    return [p], p_len


def chunk_document_semantically(text: str, max_chunk_size: int = 1200) -> list[str]:
    """
    Chunks large documents on natural structural boundaries (chapters, Surahs, Hadiths,
    page breaks, and complete sentences) to maintain context coherence and prevent cutting
    in the middle of verses or paragraphs.
    """
    if not text or not text.strip():
        return []

    raw_blocks = re.split(r"\n[ \t]*---[ \t]*\n|\n(?=#{2,3}\s)", text)
    blocks = [b.strip() for b in raw_blocks if b.strip()]

    chunks: list[str] = []
    current_chunk: list[str] = []
    current_size = 0

    for block in blocks:
        paragraphs = [p.strip() for p in block.split("\n\n") if p.strip()]
        for p in paragraphs:
            current_chunk, current_size = _ingest_paragraph_into_chunks(
                p, max_chunk_size, current_chunk, current_size, chunks
            )

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def _chunk_long_paragraph(paragraph: str, max_chunk_size: int, chunks: list[str]) -> tuple[list[str], int]:
    """
    Split a long paragraph into sentence- and verse-level sub-chunks without
    cutting in the middle of Arabic Ayahs (verse numbers, Harakat) or translations.
    """
    # Split on Latin and Arabic sentence/verse boundaries (including ۝, (1), ., ?, !, ؟)
    sentences = re.split(r"(?<=[.!?؟؛;۝\)])\s+", paragraph)
    sub_chunk: list[str] = []
    sub_size = 0
    for s in sentences:
        s_len = len(s)
        if sub_size + s_len <= max_chunk_size:
            sub_chunk.append(s)
            sub_size += s_len + 1
        else:
            if sub_chunk:
                chunks.append(" ".join(sub_chunk))
            sub_chunk = [s]
            sub_size = s_len
    if sub_chunk:
        chunks.append(" ".join(sub_chunk))
    return [], 0


def _fetch_missing_embeddings(
    missing_indices: list[int],
    missing_texts: list[str],
    model_name: str,
) -> dict[int, list[float]]:
    """Batch-fetch embeddings for texts not found in the SurrealDB cache."""
    from extractor.llm_gateway import execute_embed_content_with_fallback

    logger.info("[Embeddings] Fetching %s new embeddings from API...", len(missing_texts))
    batch_size = 20
    generated_embeddings = []
    for i in range(0, len(missing_texts), batch_size):
        batch = missing_texts[i : i + batch_size]
        try:
            response = execute_embed_content_with_fallback(model_name=model_name, contents=batch)
            for embedding_obj in response.embeddings:
                generated_embeddings.append(embedding_obj.values)
        except Exception as e:
            logger.warning("[Embeddings] Batch embedding API failed, falling back to deterministic embeddings: %s", e)
            for text in batch:
                generated_embeddings.append(generate_deterministic_embedding(text))

    return dict(zip(missing_indices, generated_embeddings, strict=False))


def _lookup_cached_embeddings(
    chunks_list: list[str], surreal_db: Any
) -> tuple[list[list[float] | None], list[int], list[str]]:
    final_embeddings: list[list[float] | None] = [None] * len(chunks_list)
    missing_indices: list[int] = []
    missing_texts: list[str] = []

    cleaned_texts = [text.strip() for text in chunks_list]
    cached_map: dict[str, list[float]] = {}
    batch_performed = False
    if hasattr(surreal_db, "find_chunk_embeddings_batch"):
        try:
            cached_map = surreal_db.find_chunk_embeddings_batch(cleaned_texts)
            batch_performed = True
        except Exception as e:
            logger.debug("[Embeddings Cache] Failed batch lookup of chunk embeddings: %s", e)

    for idx, text in enumerate(chunks_list):
        cleaned_text = cleaned_texts[idx]
        cached_vector = cached_map.get(cleaned_text)
        # Avoid N+1 query fallback if batch lookup was already executed successfully
        if not cached_vector and cleaned_text and not batch_performed and hasattr(surreal_db, "find_chunk_embedding"):
            try:
                cached_vector = surreal_db.find_chunk_embedding(cleaned_text)
            except Exception as e:
                logger.debug("[Embeddings Cache] Failed fallback lookup of chunk embedding: %s", e)

        if cached_vector:
            final_embeddings[idx] = cached_vector
        else:
            missing_indices.append(idx)
            missing_texts.append(text)
    return final_embeddings, missing_indices, missing_texts


def generate_deterministic_embedding(text: str, dimension: int = 768) -> list[float]:
    """
    Generate a normalized deterministic pseudo-embedding vector for a given text chunk.
    Used during offline mode (SURREALDB_OFFLINE=True) or when no Vertex/Gemini API keys are configured,
    guaranteeing HNSW 768 cosine compatibility without outbound API calls.
    """
    import hashlib
    import math

    if not text:
        return [0.0] * dimension

    vec: list[float] = []
    text_bytes = text.encode("utf-8")
    blocks_needed = (dimension + 7) // 8
    for i in range(blocks_needed):
        block_hash = hashlib.sha256(text_bytes + i.to_bytes(4, "big")).digest()
        for j in range(0, len(block_hash), 4):
            if len(vec) < dimension:
                val = int.from_bytes(block_hash[j : j + 4], "big", signed=True) / (2**31)
                vec.append(val)

    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0:
        return [x / norm for x in vec]
    return vec


# Sentinel value stored in place of a failed embedding so HNSW queries can
# exclude it rather than silently returning the wrong results (EDGE-01 fix).
_EMBEDDING_FAILED_SENTINEL = None


def _fill_missing_fallbacks(final_embeddings, chunks_list, model_name):
    from django.conf import settings

    from extractor.llm_gateway import execute_embed_content_with_fallback

    is_offline = getattr(settings, "SURREALDB_OFFLINE", False)

    for idx, emb in enumerate(final_embeddings):
        if emb is None:
            if is_offline:
                final_embeddings[idx] = generate_deterministic_embedding(chunks_list[idx])
                continue

            try:
                response = execute_embed_content_with_fallback(model_name=model_name, contents=[chunks_list[idx]])
                final_embeddings[idx] = response.embeddings[0].values
            except (RuntimeError, ValueError, AttributeError):
                logger.warning(
                    "[Embeddings] Live embedding failed for chunk %s — falling back to deterministic embedding.",
                    idx,
                )
                final_embeddings[idx] = generate_deterministic_embedding(chunks_list[idx])


def generate_surreal_embeddings(chunks_list: list[str], model_name: str = "text-embedding-004") -> list[list[float]]:
    """
    Fetch 768-dimension text embeddings from Google Vertex AI / AI Studio for a list of text chunks.
    Replaces the old generate_pgvector_embeddings function.
    Results are stored in SurrealDB chunks via surreal_db.recreate_chunks.
    Reuse existing vectors from SurrealDB if they exist to avoid Gemini costs.
    """
    from extractor import surreal_db

    logger.info("[Embeddings] Fetching embeddings for %s chunks...", len(chunks_list))

    final_embeddings, missing_indices, missing_texts = _lookup_cached_embeddings(chunks_list, surreal_db)

    if missing_texts:
        fetched = _fetch_missing_embeddings(missing_indices, missing_texts, model_name)
        for idx, emb in fetched.items():
            if idx < len(final_embeddings):
                final_embeddings[idx] = emb

    _fill_missing_fallbacks(final_embeddings, chunks_list, model_name)

    return [
        emb if emb is not None else generate_deterministic_embedding(chunks_list[i])
        for i, emb in enumerate(final_embeddings)
    ]


# Keep old name as alias for backward compatibility with any remaining call sites
generate_pgvector_embeddings = generate_surreal_embeddings


def _sync_postgres_memories_to_surreal(user, surreal_db, user_memory):
    pg_memories = user_memory.objects.filter(user=user)
    if pg_memories.exists():
        logger.info(
            "[Memories Sync] Restoring %s memories from PostgreSQL to SurrealDB for user %s...",
            pg_memories.count(),
            user.username,
        )
        for mem in pg_memories:
            try:
                surreal_db.add_user_memory(str(user.id), mem.memory_text, mem.embedding)
            except Exception as add_err:
                logger.warning("[Memories Sync] Failed to sync memory to SurrealDB: %s", add_err)


def _fetch_user_memories_block(user: Any, query_embedding: list[float]) -> str:
    """Fetch user long-term memories from SurrealDB (replaces Mem0)."""
    if not (user and user.is_authenticated):
        return ""
    try:
        from extractor import surreal_db
        from extractor.models import UserMemory

        try:
            db_count = surreal_db.count_user_memories(str(user.id))
        except Exception as count_err:
            logger.debug("[Memories Sync] SurrealDB memory count check failed: %s", count_err)
            db_count = 0

        from django.conf import settings

        if db_count == 0 and getattr(settings, "SURREALDB_OFFLINE", False):
            _sync_postgres_memories_to_surreal(user, surreal_db, UserMemory)

        memories = surreal_db.search_user_memories(str(user.id), query_embedding, limit=5)
        if memories:
            lines = [f"- {m.get('memory_text', '')}" for m in memories if m.get("memory_text")]
            if lines:
                return "\n[User Learning Style & Formatting Preferences]\n" + "\n".join(lines)
    except Exception as exc:
        logger.warning("[Memories] Failed to fetch user memories: %s", exc)
    return ""


PREFERENCE_RE = re.compile(
    r"\b(i\s+prefer|i\s+like|please\s+use|format\s+it|always\s+use|always\s+write|can\s+you)\b", re.IGNORECASE
)


def is_preference_signal(query: str) -> bool:
    """Regex-based heuristic — returns True if the query matches user preference patterns."""
    return bool(PREFERENCE_RE.search(query))


def _hydrate_source_from_uuid(uuid_str: str) -> dict[str, Any]:
    """Convert a bare UUID string (from rag_cache) to the source-object schema.

    The rag_cache table stores sources as UUID strings. The JS frontend expects
    dicts with at least uuid/title/language/chunk_index to render source cards.
    We do a best-effort metadata lookup; if it fails we return a minimal stub.
    """
    meta = _get_doc_metadata(uuid_str)
    return {
        "id": meta["id"],
        "uuid": uuid_str,
        "title": meta.get("title", "Unknown"),
        "author": meta.get("author", "Unknown"),
        "language": meta.get("language", "Unknown"),
        "publisher": meta.get("publisher", "Unknown"),
        "publication_year": meta.get("publication_year", ""),
        "license_type": meta.get("license_type", "Unknown"),
        "doi": meta.get("doi", ""),
        "chunk_index": 0,
        "page_number": 1,
        "chapter_title": "",
        "anchor_id": "page-1",
        "deep_link": f"/document/{uuid_str}/" if uuid_str else "",
    }


def _lookup_semantic_cache(
    user_part: str,
    cache_key: str,
    query_embedding: list[float],
    user: Any,
    document_ids: list[int] | None,
) -> dict[str, Any] | None:
    from extractor import surreal_db

    try:
        sem_hits = surreal_db.search_rag_cache_hnsw(user_part, query_embedding, threshold=0.15)
        if sem_hits:
            hit = sem_hits[0]
            # Verify all cited source UUIDs are accessible to this user
            allowed_uuids = _get_allowed_doc_uuids(user, document_ids)
            hit_sources = hit.get("sources", [])
            if allowed_uuids is None or all(s in allowed_uuids for s in hit_sources):
                logger.info("[Semantic Cache Hit] Distance <= 0.15 ($0.00 LLM cost)")
                # The rag_cache table stores sources as bare UUID strings. Hydrate
                # them into the full source-object schema that the JS frontend
                # (main.js) expects (uuid, title, language, chunk_index, etc.).
                hydrated = [_hydrate_source_from_uuid(s) for s in hit_sources]
                result = {"answer": hit["answer_text"], "sources": hydrated}
                try:
                    surreal_db.kv_cache_set(cache_key, result, ttl_seconds=86400)
                except Exception as exc:
                    logger.debug("[Semantic Cache] Failed to KV-cache hit: %s", exc)
                return result
    except Exception as exc:
        logger.warning("[Semantic Cache] Lookup failed: %s", exc)
    return None


def _save_caches(
    user_part: str,
    cache_key: str,
    query_cleaned: str,
    query_embedding: list[float],
    result: dict[str, Any],
) -> None:
    from extractor import surreal_db

    try:
        source_uuids = [s["uuid"] for s in result["sources"]]
        surreal_db.upsert_rag_cache(
            user_id=user_part,
            query_text=query_cleaned,
            query_embedding=query_embedding,
            answer_text=result["answer"],
            sources=source_uuids,
        )
    except Exception as exc:
        logger.warning("[Semantic Cache] Failed to save cache entry: %s", exc)

    try:
        surreal_db.kv_cache_set(cache_key, result, ttl_seconds=86400)
    except Exception as exc:
        logger.debug("[Semantic Cache] Failed to KV-cache result: %s", exc)


def _lookup_kv_cache(cache_key, user, document_ids, surreal_db):
    try:
        cached_result = surreal_db.kv_cache_get(cache_key)
        if cached_result:
            # Enforce access control and filter boundaries on cached KV result
            allowed_uuids = _get_allowed_doc_uuids(user, document_ids)
            cached_sources = [s.get("uuid") for s in cached_result.get("sources", [])]
            if allowed_uuids is None or all(s in allowed_uuids for s in cached_sources):
                logger.info("[Cache Hit] KV cache hit for key '%s' ($0.00 LLM cost)", cache_key)
                return cached_result
    except Exception as exc:
        logger.warning("[Cache] KV cache lookup failed: %s", exc)
    return None


def _ensure_chunks_loaded(allowed_uuids):
    if allowed_uuids is not None:
        ensure_document_chunks_loaded(allowed_uuids)
    else:
        # For admins/superusers (allowed_uuids is None), ensure chunks are loaded for all completed documents
        from django.conf import settings

        if getattr(settings, "SURREALDB_OFFLINE", False):
            from extractor.models import SourceDocument

            all_uuids = SourceDocument.objects.filter(status="COMPLETED").values_list("uuid", flat=True)
        else:
            from extractor import surreal_db

            sql = "SELECT doc_uuid FROM documents WHERE status = 'COMPLETED';"
            rows = surreal_db._first_result(surreal_db._run(sql))
            all_uuids = [r["doc_uuid"] for r in rows if "doc_uuid" in r]
        ensure_document_chunks_loaded(all_uuids)


def reciprocal_rank_fusion(
    dense_results: list[dict[str, Any]],
    sparse_results: list[dict[str, Any]],
    k: int = 60,
    top_k: int = 5,
) -> list[dict[str, Any]]:
    """
    Fuses dense vector (HNSW) search results with sparse keyword (BM25) search results
    using Reciprocal Rank Fusion (RRF).
    """
    rrf_scores: dict[str, float] = {}
    chunk_map: dict[str, dict[str, Any]] = {}

    for rank, chunk in enumerate(dense_results):
        chunk_id = str(chunk.get("id") or (str(chunk.get("doc_uuid", "")) + "_" + str(chunk.get("chunk_index", rank))))
        chunk_map[chunk_id] = chunk
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + (1.0 / (k + rank + 1))

    for rank, chunk in enumerate(sparse_results):
        chunk_id = str(chunk.get("id") or (str(chunk.get("doc_uuid", "")) + "_" + str(chunk.get("chunk_index", rank))))
        chunk_map[chunk_id] = chunk
        rrf_scores[chunk_id] = rrf_scores.get(chunk_id, 0.0) + (1.0 / (k + rank + 1))

    sorted_chunk_ids = sorted(rrf_scores.keys(), key=lambda c_id: rrf_scores[c_id], reverse=True)
    return [chunk_map[c_id] for c_id in sorted_chunk_ids[:top_k]]


def query_semantic_knowledge_rag(
    query: str,
    document_ids: list[int] | None = None,
    top_k: int = 5,
    user: Any = None,
    actor_id: str | None = None,
) -> dict[str, Any]:
    """
    Encodes the search query, runs SurrealDB Hybrid Dense-Sparse RAG chunk search (BM25 + HNSW RRF),
    and generates a grounded answer via the LLM gateway.
    """
    from extractor import cloud_tasks, surreal_db
    from extractor.llm_gateway import execute_embed_content_with_fallback
    from extractor.models import SystemSettings

    query_cleaned = query.strip()
    query_hash = hashlib.sha256(query_cleaned.lower().encode("utf-8")).hexdigest()
    user_part = actor_id or (str(user.id) if (user and user.is_authenticated) else "guest")
    cache_key = f"rag_search_cache:{user_part}:{query_hash}"

    # ── 1. Exact-match KV cache lookup (SurrealDB) ────────────────────────────
    cached_result = _lookup_kv_cache(cache_key, user, document_ids, surreal_db)
    if cached_result:
        return cached_result

    # ── 2. Fetch query embedding ───────────────────────────────────────────────
    try:
        query_emb_resp = execute_embed_content_with_fallback(model_name="text-embedding-004", contents=[query_cleaned])
        query_embedding: list[float] = query_emb_resp.embeddings[0].values
    except Exception as e:
        logger.warning("[RAG Query] Embedding API unavailable, using deterministic embedding fallback: %s", e)
        query_embedding = generate_deterministic_embedding(query_cleaned)

    # ── 3. User preference memory enqueue (async, fire-and-forget) ────────────
    if user and user.is_authenticated and is_preference_signal(query_cleaned):
        try:
            cloud_tasks.enqueue("store_user_memory", {"user_id": str(user.id), "text": query_cleaned})
        except (OSError, RuntimeError, ValueError):
            logger.debug("[Memory] Failed to enqueue memory task.")

    # ── 4. Fetch user memories from SurrealDB ─────────────────────────────────
    user_memories_block = _fetch_user_memories_block(user, query_embedding)

    # ── 5. Semantic cache lookup (SurrealDB HNSW) ─────────────────────────────
    cached_res = _lookup_semantic_cache(user_part, cache_key, query_embedding, user, document_ids)
    if cached_res:
        return cached_res

    # ── 6. SurrealDB Hybrid Dense-Sparse RAG chunk search (HNSW + BM25 RRF) ───
    allowed_uuids = _get_allowed_doc_uuids(user, document_ids, actor_id=actor_id)
    _ensure_chunks_loaded(allowed_uuids)

    try:
        dense_chunks = surreal_db.search_chunks_hnsw(query_embedding, limit=top_k, allowed_doc_uuids=allowed_uuids)
        sparse_chunks = surreal_db.search_chunks_bm25(query_cleaned, limit=top_k, allowed_doc_uuids=allowed_uuids)
        matching_chunks = reciprocal_rank_fusion(dense_chunks, sparse_chunks, k=60, top_k=top_k)
    except (OSError, RuntimeError):
        logger.exception("[RAG Search] Connection error to SurrealDB.")
        matching_chunks = []

    if not matching_chunks:
        raise ValueError("No relevant source context found in the knowledge database.")

    # ── 7. Build grounded context and resolve document metadata ───────────────
    context_str, sources = _get_grounded_context_and_sources(matching_chunks)

    try:
        settings_obj = SystemSettings.get_settings()
        selected_model = settings_obj.selected_model
    except (SystemSettings.DoesNotExist, AttributeError, RuntimeError):
        selected_model = "auto"

    # ── 8. Generate answer ────────────────────────────────────────────────────
    response = _generate_rag_answer(query_cleaned, context_str, user_memories_block, selected_model)

    result = {"answer": response.text, "sources": sources}

    # ── 9. Save to caches ─────────────────────────────────────────────────────
    _save_caches(user_part, cache_key, query_cleaned, query_embedding, result)

    return result


def _format_doc_info_parts(doc_meta: dict[str, Any], chunk: dict[str, Any] | None = None) -> str:
    """Format academic and structural metadata header for a grounded RAG context block."""
    info_parts = [
        f"Source: {doc_meta.get('title', 'Unknown')}",
        f"Lang: {doc_meta.get('language', 'Unknown')}",
        f"Author: {doc_meta.get('author', 'Unknown')}",
    ]
    if chunk:
        page_num = chunk.get("page_number")
        chap = chunk.get("chapter_title")
        if page_num:
            info_parts.append(f"Page: {page_num}")
        if chap:
            info_parts.append(f"Chapter: {chap}")

    if doc_meta.get("publisher") and doc_meta["publisher"] not in ("Unknown", ""):
        info_parts.append(f"Publisher: {doc_meta['publisher']}")
    if doc_meta.get("publication_year") and doc_meta["publication_year"] != "":
        info_parts.append(f"Year: {doc_meta['publication_year']}")
    if doc_meta.get("license_type") and doc_meta["license_type"] not in ("Unknown", ""):
        info_parts.append(f"License: {doc_meta['license_type']}")
    if doc_meta.get("doi") and doc_meta["doi"] != "":
        info_parts.append(f"DOI: {doc_meta['doi']}")
    return info_parts[0] + " (" + ", ".join(info_parts[1:]) + ")"


def _get_grounded_context_and_sources(matching_chunks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    context_blocks = []
    sources = []
    for idx, chunk in enumerate(matching_chunks):
        doc_uuid_str = str(chunk.get("doc_uuid", "") or "")
        doc_meta = _get_doc_metadata(doc_uuid_str)
        doc_info = _format_doc_info_parts(doc_meta, chunk)

        page_num = chunk.get("page_number") or 1
        chap = chunk.get("chapter_title") or ""
        anchor_id = chunk.get("anchor_id") or f"page-{page_num}"
        deep_link = f"/document/{doc_uuid_str}/#{anchor_id}" if doc_uuid_str else ""

        context_blocks.append(f"--- BLOCK {idx + 1} [{doc_info}] ---\n{chunk.get('content', '')}")
        sources.append(
            {
                "id": doc_meta["id"],
                "uuid": doc_uuid_str,
                "title": str(doc_meta.get("title", "Unknown")),
                "author": str(doc_meta.get("author", "Unknown")),
                "language": str(doc_meta.get("language", "Unknown")),
                "publisher": str(doc_meta.get("publisher", "Unknown")),
                "publication_year": str(doc_meta.get("publication_year", "")),
                "license_type": str(doc_meta.get("license_type", "Unknown")),
                "doi": str(doc_meta.get("doi", "")),
                "chunk_index": int(chunk.get("chunk_index", 0)),
                "page_number": page_num,
                "chapter_title": chap,
                "anchor_id": anchor_id,
                "deep_link": deep_link,
            }
        )
    return "\n\n".join(context_blocks), sources


def _generate_rag_answer(query_cleaned: str, context_str: str, user_memories_block: str, selected_model: str) -> Any:
    # Context Caching in SurrealDB:
    import hashlib

    from extractor import surreal_db

    if context_str:
        context_hash = hashlib.sha256(context_str.encode("utf-8")).hexdigest()
        try:
            cached_entry = surreal_db.context_cache_get(context_hash)
            if not cached_entry:
                surreal_db.context_cache_set(
                    context_hash=context_hash,
                    context_text=context_str,
                    token_count=len(context_str) // 4,
                )
        except Exception as exc:
            logger.debug("[Context Cache] surreal context cache check error: %s", exc)

    system_instruction = f"""
    You are a Digital Preservation Librarian and Archival Scholar.
    Your task is to answer the query accurately, grounding your answers ONLY in the validated source context block below.
    
    When answering, you MUST provide explicit inline academic citations (e.g., [Author, Year]) and explicitly acknowledge the legal provenance and source of the preserved literature.
    You MUST preserve the author's original meaning and intent. Do NOT summarize away nuance or change the original points.

    If the context block doesn't contain sufficient knowledge to answer, explain humbly that the context is insufficient, and do not make up external claims.
    Keep your tone highly respectful, academic, and professional.
    {user_memories_block}
    """

    rag_prompt = f"""
    Validated Source Context:
    {context_str}
    
    Query:
    {query_cleaned}
    
    Answer:
    """

    return generate_llm_content_unified(
        prompt=rag_prompt, system_instruction=system_instruction.strip(), model_name=selected_model
    )


def _parse_offline_document_ids(document_ids):
    import uuid

    uuids = []
    ids_int = []

    for x in document_ids:
        x_str = str(x).strip()
        if x_str:
            try:
                uuid.UUID(x_str)
                uuids.append(x_str)
            except ValueError:
                if x_str.isdigit():
                    ids_int.append(int(x_str))
    return uuids, ids_int


def _get_offline_uuids(user, document_ids):
    from django.db.models import Q

    from extractor.models import SourceDocument

    if not user or not user.is_authenticated:
        qs = SourceDocument.objects.filter(uploaded_by__isnull=True)
    elif not (user.is_staff or user.is_superuser):
        qs = SourceDocument.objects.filter(Q(uploaded_by=user) | Q(uploaded_by__isnull=True))
    else:
        qs = SourceDocument.objects.all()

    if document_ids:
        uuids, ids_int = _parse_offline_document_ids(document_ids)
        q_filter = Q()
        if uuids:
            q_filter |= Q(uuid__in=uuids)
        if ids_int:
            q_filter |= Q(id__in=ids_int)
        if uuids or ids_int:
            qs = qs.filter(q_filter)
    return [str(d.uuid) for d in qs]


def _get_surreal_uuids(user, document_ids, actor_id: str | None = None):
    from extractor import surreal_db

    where_clauses = []
    params: dict[str, Any] = {}
    if not user or not user.is_authenticated:
        where_clauses.append("uploaded_by_id = NONE")
    elif not (user.is_staff or user.is_superuser):
        where_clauses.append("uploaded_by_id = $user_id OR uploaded_by_id = NONE")
        params["user_id"] = actor_id or str(user.id)

    if document_ids:
        str_ids = [str(i) for i in document_ids if i]
        if str_ids:
            where_clauses.append("doc_uuid INSIDE $document_ids")
            params["document_ids"] = str_ids

    sql = "SELECT doc_uuid FROM documents"
    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)

    if user and (user.is_staff or user.is_superuser) and not document_ids:
        return None  # no filter for admins

    rows = surreal_db._first_result(surreal_db._run(sql, params))
    return [r["doc_uuid"] for r in rows]


def _get_allowed_doc_uuids(
    user: Any, document_ids: list[str] | list[int] | None, actor_id: str | None = None
) -> list[str] | None:
    """
    Returns a list of UUID strings the user is allowed to access.
    Returns None if the user is staff/superuser (no filter).
    Enforces tenant isolation on semantic cache hits and chunk searches.
    """
    from django.conf import settings

    if getattr(settings, "SURREALDB_OFFLINE", False):
        return _get_offline_uuids(user, document_ids)

    return _get_surreal_uuids(user, document_ids, actor_id=actor_id)


def _get_doc_metadata(doc_uuid: str) -> dict[str, Any]:
    """Look up document ID/title/author/language from SurrealDB by UUID."""
    from extractor import surreal_db

    try:
        doc = surreal_db.get_document(doc_uuid)
        if doc:
            raw_id = doc.get("id") or doc.get("doc_uuid") or doc_uuid
            # Always normalise to string: SurrealDB returns RecordID objects online,
            # Django returns int in offline mode. A stable string contract keeps
            # serialisation, caching, and test assertions consistent across both paths.
            doc_id = str(raw_id)
            return {
                "id": doc_id,
                "title": str(doc.get("title", "Unknown") or "Unknown"),
                "author": str(doc.get("author", "Unknown") or "Unknown"),
                "language": str(doc.get("language", "Unknown") or "Unknown"),
                "publisher": str(doc.get("publisher", "Unknown") or "Unknown"),
                "publication_year": str(doc.get("publication_year", "") or ""),
                "license_type": str(doc.get("license_type", "Unknown") or "Unknown"),
                "doi": str(doc.get("doi", "") or ""),
            }
    except Exception as exc:
        logger.debug("[Metadata] Failed to read metadata: %s", exc)
    return {
        "id": doc_uuid,
        "title": "Unknown",
        "author": "Unknown",
        "language": "Unknown",
        "publisher": "Unknown",
        "publication_year": "",
        "license_type": "Unknown",
        "doi": "",
    }


def _regenerate_chunks_for_doc(doc: dict, doc_uuid_str: str, surreal_db) -> None:
    """Regenerate and save chunks+embeddings for a single COMPLETED document that is missing them."""

    # Fall back to regeneration if JSON was missing or corrupted
    if doc.get("status") == "COMPLETED" and doc.get("refined_markdown"):
        logger.info(f"[Surreal Sync] JSON missing for COMPLETED document {doc_uuid_str}. Regenerating chunks...")
        lang = (doc.get("language") or "").lower()
        chunk_size = 500 if "arabic" in lang or "ar" in lang else 1200
        from extractor.rag import generate_surreal_embeddings
        from extractor.tasks import chunk_document_semantically

        chunks = chunk_document_semantically(doc.get("refined_markdown") or "", max_chunk_size=chunk_size)
        if chunks:
            embeddings = generate_surreal_embeddings(chunks, model_name="text-embedding-004")
            payloads = [
                {
                    "chunk_index": i,
                    "content": chunk_text,
                    "token_count": len(chunk_text.split()),
                    "language": doc.get("language") or "",
                    "embedding": emb,
                }
                for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings))
            ]
            # Save to SurrealDB
            surreal_db.recreate_chunks(doc_uuid_str, payloads)
            logger.info(f"[Surreal Sync] Regenerated and saved {len(payloads)} chunks to SurrealDB for {doc_uuid_str}.")
    else:
        logger.warning(f"[Surreal Sync] Document {doc_uuid_str} is status {doc.get('status')}, skipping sync.")


def _normalise_doc_uuid_list(doc_uuids: Any) -> list[str]:
    """Convert any UUID input (single value, iterable, or None) to a non-empty list of strings."""
    if doc_uuids is None:
        return []
    if isinstance(doc_uuids, str | uuid.UUID):
        return [str(doc_uuids)]
    try:
        return [str(u) for u in doc_uuids if str(u)]
    except TypeError:
        return [str(doc_uuids)]


def _reload_missing_chunks(missing_uuids: list[str], surreal_db: Any) -> None:
    """Regenerate chunks for documents that are present in SurrealDB but have no chunks."""
    for doc_uuid_str in missing_uuids:
        try:
            doc = surreal_db.get_document(doc_uuid_str)
            if not doc:
                continue
        except (OSError, ValueError, RuntimeError) as doc_err:
            logger.debug("[Surreal Sync] Skipping %s: %s", doc_uuid_str, doc_err)
            continue
        try:
            _regenerate_chunks_for_doc(doc, doc_uuid_str, surreal_db)
        except Exception as exc:
            logger.warning("[Surreal Sync] Failed to ensure chunks for %s: %s", doc_uuid_str, exc)


def ensure_document_chunks_loaded(doc_uuids: Any) -> None:
    """
    Checks if chunks for doc_uuids exist in SurrealDB.
    If they are missing and the document is COMPLETED, this function regenerates the chunks
    and embeddings on-the-fly.
    """
    from extractor import surreal_db

    doc_uuid_strs = _normalise_doc_uuid_list(doc_uuids)
    if not doc_uuid_strs:
        return

    try:
        counts = surreal_db.count_documents_chunks(doc_uuid_strs)
    except Exception as exc:
        logger.warning("[Surreal Sync] Failed to check chunks count for list: %s", exc)
        counts = {}

    missing_uuids = [u for u in doc_uuid_strs if counts.get(u, 0) == 0]
    if missing_uuids:
        _reload_missing_chunks(missing_uuids, surreal_db)
