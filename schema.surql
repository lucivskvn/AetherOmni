-- ============================================================
-- AetherOmni — SurrealDB Schema (v3.x compatible)
-- Apply with:  surreal import --conn wss://surrealdb.fainko.cloud/rpc \
--              --user <user> --pass <pass> --ns aetheromni --db extractor \
--              schema.surql
--
-- For local dev (docker-compose):
--              surreal import --conn http://localhost:8001 \
--              --user root --pass root --ns aetheromni --db extractor \
--              schema.surql
-- ============================================================

-- ── 1. documents ─────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS documents SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid          ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS uploaded_by_id    ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS file              ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS original_filename ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS file_hash         ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS status            ON documents TYPE string;          -- PENDING | EXTRACTING | REFINING | EMBEDDING | COMPLETED | FAILED
DEFINE FIELD IF NOT EXISTS error_message     ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS language          ON documents TYPE string DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS author            ON documents TYPE string DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS title             ON documents TYPE string DEFAULT "Untitled";
DEFINE FIELD IF NOT EXISTS document_type     ON documents TYPE string DEFAULT "PDF";
DEFINE FIELD IF NOT EXISTS page_count        ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS publisher         ON documents TYPE option<string> DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS publication_year  ON documents TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS license_type      ON documents TYPE option<string> DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS doi               ON documents TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS raw_markdown      ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS refined_markdown  ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS yaml_metadata     ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS qa_dataset        ON documents TYPE any  DEFAULT [];
DEFINE FIELD IF NOT EXISTS input_tokens      ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS output_tokens     ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS cost_usd          ON documents TYPE float  DEFAULT 0.0;
DEFINE FIELD IF NOT EXISTS semantic_signature ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS retry_count       ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS expires_at        ON documents TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at        ON documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at        ON documents TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_documents_uuid ON documents FIELDS doc_uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_documents_hash ON documents FIELDS file_hash;


-- ── 2. chunks ─────────────────────────────────────────────────
-- Vector store for all document text chunks.
-- HNSW index provides sub-millisecond approximate-nearest-neighbour search.
DEFINE TABLE IF NOT EXISTS chunks SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid        ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_index     ON chunks TYPE int;
DEFINE FIELD IF NOT EXISTS content         ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS token_count     ON chunks TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS language        ON chunks TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS embedding       ON chunks TYPE array<float>;
DEFINE FIELD IF NOT EXISTS created_at      ON chunks TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_chunks_doc  ON chunks FIELDS doc_uuid;
-- HNSW index (1536 dims = text-embedding-004 output, cosine similarity)
DEFINE INDEX IF NOT EXISTS idx_chunks_hnsw ON chunks FIELDS embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 3. rag_cache ──────────────────────────────────────────────
-- Semantic query cache.  A cache hit short-circuits Vertex AI calls.
-- Entries are TTL-pruned during scheduled cleanup runs.
DEFINE TABLE IF NOT EXISTS rag_cache SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS query_text      ON rag_cache TYPE string;
DEFINE FIELD IF NOT EXISTS query_embedding ON rag_cache TYPE array<float>;
DEFINE FIELD IF NOT EXISTS answer_text     ON rag_cache TYPE string;
DEFINE FIELD IF NOT EXISTS sources         ON rag_cache TYPE array<string>;   -- list of doc_uuid strings
DEFINE FIELD IF NOT EXISTS user_id         ON rag_cache TYPE string;          -- tenant isolation
DEFINE FIELD IF NOT EXISTS created_at      ON rag_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at      ON rag_cache TYPE datetime DEFAULT time::now() + 7d;

DEFINE INDEX IF NOT EXISTS idx_rag_cache_user ON rag_cache FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_rag_cache_hnsw ON rag_cache FIELDS query_embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 4. kv_cache ───────────────────────────────────────────────
-- General-purpose key-value cache replacing Redis for small payloads
-- (exchange rates, health check results, etc.)
DEFINE TABLE IF NOT EXISTS kv_cache SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS cache_key       ON kv_cache TYPE string;
DEFINE FIELD IF NOT EXISTS cache_value     ON kv_cache TYPE string;           -- JSON-encoded value
DEFINE FIELD IF NOT EXISTS expires_at      ON kv_cache TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at      ON kv_cache TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_kv_cache_key ON kv_cache FIELDS cache_key UNIQUE;


-- ── 5. audit_logs ─────────────────────────────────────────────
-- Immutable compliance audit trail.  Dual-written from Django ORM
-- (SQLite) + SurrealDB for long-term retention.
DEFINE TABLE IF NOT EXISTS audit_logs SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS action          ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS user_id         ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS doc_uuid        ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS metadata        ON audit_logs TYPE string DEFAULT "{}";  -- JSON blob
DEFINE FIELD IF NOT EXISTS ip_address      ON audit_logs TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS timestamp       ON audit_logs TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_audit_user  ON audit_logs FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_audit_time  ON audit_logs FIELDS timestamp;


-- ── 6. user_memories ─────────────────────────────────────────
-- Replaces Mem0 — stores per-user memory blobs retrieved during RAG.
DEFINE TABLE IF NOT EXISTS user_memories SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS user_id         ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS memory_text     ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS embedding       ON user_memories TYPE array<float>;
DEFINE FIELD IF NOT EXISTS created_at      ON user_memories TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mem_user    ON user_memories FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_mem_hnsw    ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 7. system_settings ───────────────────────────────────────
DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS monthly_budget_usd ON system_settings TYPE float DEFAULT 10.00;
DEFINE FIELD IF NOT EXISTS selected_model     ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS currency           ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS openrouter_api_key ON system_settings TYPE string DEFAULT "";
