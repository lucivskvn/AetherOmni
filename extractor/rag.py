# Copyright (c) 2026 Knowledge Desk Contributors.
# All rights reserved. Confidential and Proprietary.

from __future__ import annotations

import hashlib
import logging
import re
import uuid
from typing import Any

from extractor.llm_gateway import generate_llm_content_unified

logger = logging.getLogger(__name__)

GEMINI_API_KEY_ERROR = "GEMINI_API_KEY is not configured."


def chunk_document_semantically(text: str, max_chunk_size: int = 1200) -> list[str]:
    """
    Chunks large documents on natural boundaries (paragraphs or sentences)
    to maintain context coherence during semantic memory searches.
    Gap H-3: max_chunk_size is passed by the caller — tasks.py uses 500 for Arabic,
    1200 for Latin/Latin-script languages.
    """
    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current_chunk: list[str] = []
    current_size = 0

    for p in paragraphs:
        p_len = len(p)
        if current_size + p_len <= max_chunk_size:
            current_chunk.append(p)
            current_size += p_len + 2  # account for double newline
        else:
            if current_chunk:
                chunks.append("\n\n".join(current_chunk))

            if p_len > max_chunk_size:
                sub_chunk, sub_size = _chunk_long_paragraph(p, max_chunk_size, chunks)
                current_chunk = sub_chunk
                current_size = sub_size
            else:
                current_chunk = [p]
                current_size = p_len

    if current_chunk:
        chunks.append("\n\n".join(current_chunk))

    return chunks


def _chunk_long_paragraph(paragraph: str, max_chunk_size: int, chunks: list[str]) -> tuple[list[str], int]:
    """Split a very long paragraph into sentence-level sub-chunks."""
    sentences = re.split(r"(?<=[.!?])\s+", paragraph)
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
    return sub_chunk, sub_size


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
        response = execute_embed_content_with_fallback(model_name=model_name, contents=batch)
        for embedding_obj in response.embeddings:
            generated_embeddings.append(embedding_obj.values)

    return {idx: emb for idx, emb in zip(missing_indices, generated_embeddings)}


def _lookup_cached_embeddings(chunks_list, surreal_db):
    final_embeddings = [None] * len(chunks_list)
    missing_indices = []
    missing_texts = []

    for idx, text in enumerate(chunks_list):
        cleaned_text = text.strip()
        cached_vector = None
        if cleaned_text:
            try:
                cached_vector = surreal_db.find_chunk_embedding(cleaned_text)
            except Exception as e:
                logger.debug("[Embeddings Cache] Failed to look up chunk embedding: %s", e)

        if cached_vector:
            final_embeddings[idx] = cached_vector
        else:
            missing_indices.append(idx)
            missing_texts.append(text)
    return final_embeddings, missing_indices, missing_texts


def _fill_missing_fallbacks(final_embeddings, chunks_list, model_name):
    from extractor.llm_gateway import execute_embed_content_with_fallback

    for idx, emb in enumerate(final_embeddings):
        if emb is None:
            try:
                response = execute_embed_content_with_fallback(model_name=model_name, contents=[chunks_list[idx]])
                final_embeddings[idx] = response.embeddings[0].values
            except Exception:
                final_embeddings[idx] = [0.0] * 768


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

    return final_embeddings


# Keep old name as alias for backward compatibility with any remaining call sites
generate_pgvector_embeddings = generate_surreal_embeddings


def _sync_postgres_memories_to_surreal(user, surreal_db, UserMemory):
    pg_memories = UserMemory.objects.filter(user=user)
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

        if db_count == 0:
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
            # Gap F-8: verify all cited source UUIDs are accessible to this user
            allowed_uuids = _get_allowed_doc_uuids(user, document_ids)
            hit_sources = hit.get("sources", [])
            if allowed_uuids is None or all(s in allowed_uuids for s in hit_sources):
                logger.info("[Semantic Cache Hit] Distance <= 0.15 ($0.00 LLM cost)")
                result = {"answer": hit["answer_text"], "sources": hit_sources}
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
            # Enforce access control and filter boundaries on cached KV result (Gap F-8)
            allowed_uuids = _get_allowed_doc_uuids(user, document_ids)
            cached_sources = [s.get("uuid") for s in cached_result.get("sources", [])]
            if allowed_uuids is None or all(s in allowed_uuids for s in cached_sources):
                logger.info("[Cache Hit] KV cache hit for key '%s' ($0.00 LLM cost)", cache_key)
                return cached_result
    except Exception as exc:
        logger.warning("[Cache] KV cache lookup failed: %s", exc)
    return None


def _ensure_chunks_loaded_for_user(user, allowed_uuids):
    if allowed_uuids is not None:
        ensure_document_chunks_loaded(allowed_uuids)
    else:
        # For admins/superusers (allowed_uuids is None), ensure chunks are loaded for all completed documents
        from extractor.models import SourceDocument

        all_uuids = SourceDocument.objects.filter(status="COMPLETED").values_list("uuid", flat=True)
        ensure_document_chunks_loaded(all_uuids)


def query_semantic_knowledge_rag(
    query: str,
    document_ids: list[int] | None = None,
    top_k: int = 5,
    user: Any = None,
) -> dict[str, Any]:
    """
    Encodes the search query, runs SurrealDB HNSW chunk search, and generates a
    grounded answer via the LLM gateway.

    Improvements in v2.0:
    - D-1: SurrealDB HNSW chunk search (replaces pgvector).
    - D-2: SurrealDB semantic cache lookup (replaces pgvector RAGQueryCache).
    - D-3: SurrealDB KV exact-match cache (replaces Redis).
    - D-4/D-5: SurrealDB user memories (replaces Mem0 / Django-Q memory tasks).
    - F-8: tenant isolation — cache hits verified against user's allowed documents.
    - E-29: sources include doc UUID string for correct frontend URL generation.
    - H-3: chunk size handled by caller (arabic-aware).
    """
    from extractor import cloud_tasks, surreal_db
    from extractor.llm_gateway import execute_embed_content_with_fallback
    from extractor.models import SystemSettings

    query_cleaned = query.strip()
    query_hash = hashlib.sha256(query_cleaned.lower().encode("utf-8")).hexdigest()
    user_part = str(user.id) if (user and user.is_authenticated) else "guest"
    cache_key = f"rag_search_cache:{user_part}:{query_hash}"

    # ── 1. Exact-match KV cache lookup (SurrealDB) ────────────────────────────
    cached_result = _lookup_kv_cache(cache_key, user, document_ids, surreal_db)
    if cached_result:
        return cached_result

    # ── 2. Fetch query embedding ───────────────────────────────────────────────
    query_emb_resp = execute_embed_content_with_fallback(model_name="text-embedding-004", contents=[query_cleaned])
    query_embedding: list[float] = query_emb_resp.embeddings[0].values

    # ── 3. User preference memory enqueue (async, fire-and-forget) ────────────
    if user and user.is_authenticated and is_preference_signal(query_cleaned):
        try:
            cloud_tasks.enqueue("store_user_memory", {"user_id": str(user.id), "text": query_cleaned})
        except Exception:
            logger.debug("[Memory] Failed to enqueue memory task.")

    # ── 4. Fetch user memories from SurrealDB ─────────────────────────────────
    user_memories_block = _fetch_user_memories_block(user, query_embedding)

    # ── 5. Semantic cache lookup (SurrealDB HNSW) ─────────────────────────────
    cached_res = _lookup_semantic_cache(user_part, cache_key, query_embedding, user, document_ids)
    if cached_res:
        return cached_res

    # ── 6. SurrealDB HNSW chunk search ────────────────────────────────────────
    allowed_uuids = _get_allowed_doc_uuids(user, document_ids)
    _ensure_chunks_loaded_for_user(user, allowed_uuids)

    try:
        matching_chunks = surreal_db.search_chunks_hnsw(query_embedding, limit=top_k, allowed_doc_uuids=allowed_uuids)
    except Exception:
        logger.exception("[RAG Search] Connection error to SurrealDB.")
        matching_chunks = []

    if not matching_chunks:
        raise ValueError("No relevant source context found in the knowledge database.")

    # ── 7. Build grounded context and resolve document metadata ───────────────
    context_str, sources = _get_grounded_context_and_sources(matching_chunks)

    try:
        settings_obj = SystemSettings.get_settings()
        selected_model = settings_obj.selected_model
    except Exception:
        selected_model = "auto"

    # ── 8. Generate answer ────────────────────────────────────────────────────
    response = _generate_rag_answer(query_cleaned, context_str, user_memories_block, selected_model)

    result = {"answer": response.text, "sources": sources}

    # ── 9. Save to caches ─────────────────────────────────────────────────────
    _save_caches(user_part, cache_key, query_cleaned, query_embedding, result)

    return result


def _get_grounded_context_and_sources(matching_chunks: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
    context_blocks = []
    sources = []
    for idx, chunk in enumerate(matching_chunks):
        doc_uuid_str = chunk.get("doc_uuid", "")
        # Look up SQLite doc for title/author/language (UUID is the join key)
        doc_meta = _get_doc_metadata(doc_uuid_str)
        doc_info = f"Source: {doc_meta['title']} (Lang: {doc_meta['language']}, Author: {doc_meta['author']})"
        context_blocks.append(f"--- BLOCK {idx + 1} [{doc_info}] ---\n{chunk.get('content', '')}")
        sources.append(
            {
                "id": doc_meta["id"],  # For test compatibility (SQLite integer ID)
                "uuid": doc_uuid_str,  # Gap E-29: use UUID for correct frontend URL routing
                "title": doc_meta["title"],
                "author": doc_meta["author"],
                "language": doc_meta["language"],
                "chunk_index": chunk.get("chunk_index", 0),
            }
        )
    return "\n\n".join(context_blocks), sources


def _generate_rag_answer(query_cleaned: str, context_str: str, user_memories_block: str, selected_model: str) -> Any:
    system_instruction = f"""
    You are an advanced, objective Q&A knowledge engine designed to serve the Ummah.
    Your task is to answer the query accurately, grounding your answers ONLY in the validated source context block below.
    
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


def _get_allowed_doc_uuids(user: Any, document_ids: list[int] | None) -> list[str] | None:
    """
    Returns a list of UUID strings the user is allowed to access.
    Returns None if the user is staff/superuser (no filter).
    Gap F-8: enforces tenant isolation on semantic cache hits and chunk searches.
    """
    from extractor.models import SourceDocument

    qs = SourceDocument.objects.all()
    if not user or not user.is_authenticated:
        # Unauthenticated users can only see public documents
        qs = qs.filter(uploaded_by__isnull=True)
    elif not (user.is_staff or user.is_superuser):
        from django.db.models import Q

        qs = qs.filter(Q(uploaded_by=user) | Q(uploaded_by__isnull=True))
    if document_ids:
        qs = qs.filter(id__in=document_ids)

    if user and (user.is_staff or user.is_superuser) and not document_ids:
        return None  # no filter for admins

    return [str(doc.uuid) for doc in qs.only("uuid")]


def _get_doc_metadata(doc_uuid: str) -> dict[str, Any]:
    """Look up document ID/title/author/language from SQLite by UUID."""
    from extractor.models import SourceDocument

    try:
        doc = SourceDocument.objects.filter(uuid=doc_uuid).values("id", "title", "author", "language").first()
        if doc:
            return doc
    except Exception as exc:
        logger.debug("[Metadata] Failed to read metadata: %s", exc)
    return {"id": None, "title": "Unknown", "author": "Unknown", "language": "Unknown"}


def _regenerate_chunks_for_doc(doc, doc_uuid_str, surreal_db, default_storage, json, ContentFile) -> None:
    """Regenerate and save chunks+embeddings for a single COMPLETED document that is missing them."""
    if doc.status == "COMPLETED" and doc.refined_markdown:
        logger.info(f"[Surreal Sync] JSON missing for COMPLETED document {doc_uuid_str}. Regenerating chunks...")
        lang = (doc.language or "").lower()
        chunk_size = 500 if "arabic" in lang or "ar" in lang else 1200
        from extractor.rag import generate_surreal_embeddings
        from extractor.tasks import chunk_document_semantically

        chunks = chunk_document_semantically(doc.refined_markdown, max_chunk_size=chunk_size)
        if chunks:
            embeddings = generate_surreal_embeddings(chunks, model_name="text-embedding-004")
            payloads = [
                {
                    "chunk_index": i,
                    "content": chunk_text,
                    "token_count": len(chunk_text.split()),
                    "language": doc.language or "",
                    "embedding": emb,
                }
                for i, (chunk_text, emb) in enumerate(zip(chunks, embeddings))
            ]
            # Save to SurrealDB
            surreal_db.recreate_chunks(doc_uuid_str, payloads)
            # Save to storage JSON
            chunks_json_path = f"chunks/{doc_uuid_str}.json"
            default_storage.save(chunks_json_path, ContentFile(json.dumps(payloads).encode("utf-8")))
            logger.info(
                f"[Surreal Sync] Regenerated and saved {len(payloads)} chunks to storage and SurrealDB for {doc_uuid_str}."
            )
    else:
        logger.warning(f"[Surreal Sync] Document {doc_uuid_str} is status {doc.status}, skipping sync.")


def ensure_document_chunks_loaded(doc_uuids: Any) -> None:
    """
    Checks if chunks for doc_uuids exist in SurrealDB.
    If not, attempts to download the JSON manifest from GCS/storage and load it.
    If the JSON manifest is also missing from GCS but the document is COMPLETED,
    this function regenerates the chunks and embeddings on-the-fly and saves them to GCS.
    """
    import json

    from django.core.files.base import ContentFile
    from django.core.files.storage import default_storage

    from extractor import surreal_db
    from extractor.models import SourceDocument

    if doc_uuids is None:
        return

    if isinstance(doc_uuids, str | uuid.UUID):
        doc_uuid_strs = [str(doc_uuids)]
    else:
        try:
            doc_uuid_strs = [str(u) for u in doc_uuids]
        except TypeError:
            doc_uuid_strs = [str(doc_uuids)]

    doc_uuid_strs = [u for u in doc_uuid_strs if u]
    if not doc_uuid_strs:
        return

    try:
        # 1. Check SurrealDB count in bulk
        counts = surreal_db.count_documents_chunks(doc_uuid_strs)
    except Exception as exc:
        logger.warning(f"[Surreal Sync] Failed to check chunks count for list: {exc}")
        counts = {}

    missing_uuids = [u for u in doc_uuid_strs if counts.get(u, 0) == 0]
    if not missing_uuids:
        return

    for doc_uuid_str in missing_uuids:
        try:
            doc = SourceDocument.objects.get(uuid=doc_uuid_str)
        except SourceDocument.DoesNotExist:
            continue

        try:
            _regenerate_chunks_for_doc(doc, doc_uuid_str, surreal_db, default_storage, json, ContentFile)
        except Exception as exc:
            logger.warning(f"[Surreal Sync] Failed to ensure chunks for {doc_uuid_str}: {exc}")
