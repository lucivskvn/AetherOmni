-- ============================================================
-- AetherOmni — SurrealDB Schema v2.0
-- Apply with:  surreal import --conn http://localhost:8001 \
--              --user root --pass root --ns omnirag --db extractor \
--              schema.surql
-- ============================================================

-- ── 1. documents ─────────────────────────────────────────────
-- Mirrors SourceDocument (SQLite) for fast KV look-ups from
-- worker tasks without hitting Django ORM.
DEFINE TABLE documents SCHEMAFULL;

DEFINE FIELD doc_uuid        ON documents TYPE string;
DEFINE FIELD status          ON documents TYPE string;          -- PENDING | EXTRACTING | REFINING | EMBEDDING | COMPLETED | FAILED
DEFINE FIELD filename        ON documents TYPE string;
DEFINE FIELD language        ON documents TYPE string DEFAULT "";
DEFINE FIELD page_count      ON documents TYPE int    DEFAULT 0;
DEFINE FIELD expires_at      ON documents TYPE option<datetime>;
DEFINE FIELD created_at      ON documents TYPE datetime DEFAULT time::now();
DEFINE FIELD updated_at      ON documents TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_documents_uuid ON documents FIELDS doc_uuid UNIQUE;


-- ── 2. chunks ─────────────────────────────────────────────────
-- Vector store for all document text chunks.
-- HNSW index provides sub-millisecond approximate-nearest-neighbour search.
DEFINE TABLE chunks SCHEMAFULL;

DEFINE FIELD doc_uuid        ON chunks TYPE string;
DEFINE FIELD chunk_index     ON chunks TYPE int;
DEFINE FIELD content         ON chunks TYPE string;
DEFINE FIELD token_count     ON chunks TYPE int    DEFAULT 0;
DEFINE FIELD language        ON chunks TYPE string DEFAULT "";
DEFINE FIELD embedding       ON chunks TYPE array<float>;
DEFINE FIELD created_at      ON chunks TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_chunks_doc  ON chunks FIELDS doc_uuid;
-- HNSW index (1536 dims = text-embedding-004 output, cosine similarity)
DEFINE INDEX idx_chunks_hnsw ON chunks FIELDS embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 3. rag_cache ──────────────────────────────────────────────
-- Semantic query cache.  A cache hit short-circuits Vertex AI calls.
-- Entries are TTL-pruned during scheduled cleanup runs.
DEFINE TABLE rag_cache SCHEMAFULL;

DEFINE FIELD query_text      ON rag_cache TYPE string;
DEFINE FIELD query_embedding ON rag_cache TYPE array<float>;
DEFINE FIELD answer_text     ON rag_cache TYPE string;
DEFINE FIELD sources         ON rag_cache TYPE array<string>;   -- list of doc_uuid strings
DEFINE FIELD user_id         ON rag_cache TYPE string;          -- tenant isolation
DEFINE FIELD created_at      ON rag_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD expires_at      ON rag_cache TYPE datetime DEFAULT time::now() + 7d;

DEFINE INDEX idx_rag_cache_user ON rag_cache FIELDS user_id;
DEFINE INDEX idx_rag_cache_hnsw ON rag_cache FIELDS query_embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 4. kv_cache ───────────────────────────────────────────────
-- General-purpose key-value cache replacing Redis for small payloads
-- (exchange rates, health check results, etc.)
DEFINE TABLE kv_cache SCHEMAFULL;

DEFINE FIELD cache_key       ON kv_cache TYPE string;
DEFINE FIELD cache_value     ON kv_cache TYPE string;           -- JSON-encoded value
DEFINE FIELD expires_at      ON kv_cache TYPE option<datetime>;
DEFINE FIELD created_at      ON kv_cache TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_kv_cache_key ON kv_cache FIELDS cache_key UNIQUE;


-- ── 5. audit_logs ─────────────────────────────────────────────
-- Immutable compliance audit trail.  Dual-written from Django ORM
-- (SQLite) + SurrealDB for long-term retention.
DEFINE TABLE audit_logs SCHEMAFULL;

DEFINE FIELD action          ON audit_logs TYPE string;
DEFINE FIELD user_id         ON audit_logs TYPE string;
DEFINE FIELD doc_uuid        ON audit_logs TYPE option<string>;
DEFINE FIELD metadata        ON audit_logs TYPE string DEFAULT "{}";  -- JSON blob
DEFINE FIELD ip_address      ON audit_logs TYPE string DEFAULT "";
DEFINE FIELD timestamp       ON audit_logs TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_audit_user  ON audit_logs FIELDS user_id;
DEFINE INDEX idx_audit_time  ON audit_logs FIELDS timestamp;


-- ── 6. user_memories ─────────────────────────────────────────
-- Replaces Mem0 — stores per-user memory blobs retrieved during RAG.
DEFINE TABLE user_memories SCHEMAFULL;

DEFINE FIELD user_id         ON user_memories TYPE string;
DEFINE FIELD memory_text     ON user_memories TYPE string;
DEFINE FIELD embedding       ON user_memories TYPE array<float>;
DEFINE FIELD created_at      ON user_memories TYPE datetime DEFAULT time::now();

DEFINE INDEX idx_mem_user    ON user_memories FIELDS user_id;
DEFINE INDEX idx_mem_hnsw    ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;
