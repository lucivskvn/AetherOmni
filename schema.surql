-- ============================================================
-- KORDA — SurrealDB Schema (v3.x compatible)
-- Apply with:  surreal import --conn $SURREAL_URL \
--              --user <user> --pass <pass> --ns korda --db extractor \
--              schema.surql
--
-- For local dev (docker-compose):
--              surreal import --conn http://localhost:8001 \
--              --user root --pass root --ns korda --db extractor \
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
DEFINE INDEX IF NOT EXISTS idx_documents_status ON documents FIELDS status;
DEFINE INDEX IF NOT EXISTS idx_documents_user   ON documents FIELDS uploaded_by_id;
DEFINE INDEX IF NOT EXISTS idx_documents_created ON documents FIELDS created_at;

-- ── 2. chunks ─────────────────────────────────────────────────
-- Vector store for all document text chunks.
-- HNSW index provides sub-millisecond approximate-nearest-neighbour search.
DEFINE TABLE IF NOT EXISTS chunks SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid        ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_index     ON chunks TYPE int;
DEFINE FIELD IF NOT EXISTS content         ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS token_count     ON chunks TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS language        ON chunks TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS page_number     ON chunks TYPE option<int> DEFAULT 1;
DEFINE FIELD IF NOT EXISTS chapter_title   ON chunks TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS anchor_id       ON chunks TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS embedding       ON chunks TYPE array<float>;
DEFINE FIELD IF NOT EXISTS created_at      ON chunks TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_chunks_doc  ON chunks FIELDS doc_uuid;
-- HNSW index (768 dims = embedding model output, cosine similarity)
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
DEFINE INDEX IF NOT EXISTS idx_rag_cache_exp  ON rag_cache FIELDS expires_at;
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
DEFINE INDEX IF NOT EXISTS idx_kv_cache_exp ON kv_cache FIELDS expires_at;


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

DEFINE FIELD IF NOT EXISTS user_id          ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS memory_text      ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS category         ON user_memories TYPE string DEFAULT "general";
DEFINE FIELD IF NOT EXISTS confidence       ON user_memories TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS embedding        ON user_memories TYPE array<float>;
DEFINE FIELD IF NOT EXISTS last_accessed_at ON user_memories TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at       ON user_memories TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mem_user     ON user_memories FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_mem_cat      ON user_memories FIELDS category;
DEFINE INDEX IF NOT EXISTS idx_mem_hnsw     ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 7. system_settings ───────────────────────────────────────
DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS monthly_budget_usd   ON system_settings TYPE float  DEFAULT 10.00;
DEFINE FIELD IF NOT EXISTS selected_model       ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS currency             ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS csrf_trusted_origins ON system_settings TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS openrouter_api_key   ON system_settings TYPE string DEFAULT "";


-- ── 8. context_cache ─────────────────────────────────────────
-- Caches tokenized prompt contexts and long-context document prefixes.
DEFINE TABLE IF NOT EXISTS context_cache SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS context_hash    ON context_cache TYPE string;
DEFINE FIELD IF NOT EXISTS doc_uuid        ON context_cache TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_id         ON context_cache TYPE option<string>;
DEFINE FIELD IF NOT EXISTS context_text    ON context_cache TYPE string;
DEFINE FIELD IF NOT EXISTS token_count     ON context_cache TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS hit_count       ON context_cache TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS expires_at      ON context_cache TYPE datetime DEFAULT time::now() + 3d;
DEFINE FIELD IF NOT EXISTS created_at      ON context_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at      ON context_cache TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_context_hash ON context_cache FIELDS context_hash UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_context_doc  ON context_cache FIELDS doc_uuid;
DEFINE INDEX IF NOT EXISTS idx_context_exp  ON context_cache FIELDS expires_at;


-- ── 9. rate_limits ───────────────────────────────────────────
-- Distributed atomic token & request sliding-window rate limiting.
DEFINE TABLE IF NOT EXISTS rate_limits SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS key             ON rate_limits TYPE string;        -- user_id or client_ip:window
DEFINE FIELD IF NOT EXISTS request_count   ON rate_limits TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS token_count     ON rate_limits TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS window_start    ON rate_limits TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at      ON rate_limits TYPE datetime DEFAULT time::now() + 1h;

DEFINE INDEX IF NOT EXISTS idx_rate_limits_key ON rate_limits FIELDS key UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_rate_limits_exp ON rate_limits FIELDS expires_at;


-- ── 10. entities (Knowledge Graph Nodes) ───────────────────────
-- Multi-tenant entity nodes for Graph Relational RAG.
DEFINE TABLE IF NOT EXISTS entities SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS name        ON entities TYPE string;
DEFINE FIELD IF NOT EXISTS category    ON entities TYPE string DEFAULT "CONCEPT"; -- CONCEPT | PERSON | ORG | LAW | LOCATION
DEFINE FIELD IF NOT EXISTS description ON entities TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS tenant_id   ON entities TYPE string;                  -- Supabase user/org UUID
DEFINE FIELD IF NOT EXISTS embedding   ON entities TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at  ON entities TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_entity_name ON entities FIELDS name, tenant_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_entity_user ON entities FIELDS tenant_id;
DEFINE INDEX IF NOT EXISTS idx_entity_hnsw ON entities FIELDS embedding HNSW DIMENSION 768 DIST COSINE;


-- ── 11. chunk_references (Graph Relation Edge: chunks -> entities)
-- Links text chunks to extracted knowledge graph entities.
DEFINE TABLE IF NOT EXISTS chunk_references SCHEMAFULL TYPE RELATION FROM chunks TO entities;

DEFINE FIELD IF NOT EXISTS relevance_score ON chunk_references TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS extracted_at    ON chunk_references TYPE datetime DEFAULT time::now();


-- ── 12. entity_relations (Graph Relation Edge: entities -> entities)
-- Inter-concept relationship edges within tenant boundary.
DEFINE TABLE IF NOT EXISTS entity_relations SCHEMAFULL TYPE RELATION FROM entities TO entities;

DEFINE FIELD IF NOT EXISTS relation_type   ON entity_relations TYPE string DEFAULT "RELATED_TO"; -- CITES | GOVERNS | PART_OF | AMENDS
DEFINE FIELD IF NOT EXISTS confidence      ON entity_relations TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS tenant_id       ON entity_relations TYPE string;
DEFINE FIELD IF NOT EXISTS created_at      ON entity_relations TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_rel_tenant  ON entity_relations FIELDS tenant_id;
