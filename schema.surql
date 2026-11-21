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
DEFINE FIELD IF NOT EXISTS cloud_task_name   ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS cancel_requested  ON documents TYPE bool DEFAULT false;
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
DEFINE FIELD IF NOT EXISTS embedding       ON chunks TYPE option<array<float>>;
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


-- ── 5. document_spend_reconciliations ─────────────────────────
-- Durable, document-scoped deletion spend ledger.  The unique document key
-- makes a retried purge idempotent without relying on a mutable cache value.
DEFINE TABLE IF NOT EXISTS document_spend_reconciliations SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid      ON document_spend_reconciliations TYPE string;
DEFINE FIELD IF NOT EXISTS year          ON document_spend_reconciliations TYPE int;
DEFINE FIELD IF NOT EXISTS month         ON document_spend_reconciliations TYPE int;
DEFINE FIELD IF NOT EXISTS cost_usd      ON document_spend_reconciliations TYPE float;
DEFINE FIELD IF NOT EXISTS input_tokens  ON document_spend_reconciliations TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS output_tokens ON document_spend_reconciliations TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS reconciled_at ON document_spend_reconciliations TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_spend_reconciliation_doc ON document_spend_reconciliations FIELDS doc_uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_spend_reconciliation_month ON document_spend_reconciliations FIELDS year, month;


-- ── 5b. monthly_spend_ledger ─────────────────────────────────
-- Append-only ledger for non-document spend events (e.g. Eventarc KORDA
-- lanes). Each event is an immutable INSERT so concurrent writers cannot
-- lose increments through a read-modify-write on a shared KV value.
DEFINE TABLE IF NOT EXISTS monthly_spend_ledger SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS year          ON monthly_spend_ledger TYPE int;
DEFINE FIELD IF NOT EXISTS month         ON monthly_spend_ledger TYPE int;
DEFINE FIELD IF NOT EXISTS cost_usd      ON monthly_spend_ledger TYPE float;
DEFINE FIELD IF NOT EXISTS input_tokens  ON monthly_spend_ledger TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS output_tokens ON monthly_spend_ledger TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS recorded_at   ON monthly_spend_ledger TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_monthly_spend_ledger_month ON monthly_spend_ledger FIELDS year, month;


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
DEFINE INDEX IF NOT EXISTS idx_audit_doc   ON audit_logs FIELDS doc_uuid;
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


-- ============================================================
-- KORDA Lane 1: Knowledge Refinery Provenance Graph
-- ============================================================

-- ── 13. source (Canonical Multimodal Asset & Tombstone) ───────
DEFINE TABLE IF NOT EXISTS source SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS source_id       ON source TYPE string;
DEFINE FIELD IF NOT EXISTS doc_slug        ON source TYPE string;
DEFINE FIELD IF NOT EXISTS owner_id        ON source TYPE string;
DEFINE FIELD IF NOT EXISTS title           ON source TYPE string DEFAULT "Untitled";
DEFINE FIELD IF NOT EXISTS mime_type       ON source TYPE string DEFAULT "application/pdf";
DEFINE FIELD IF NOT EXISTS gcs_uri         ON source TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS gcs_status      ON source TYPE string DEFAULT "active"; -- active | purged
DEFINE FIELD IF NOT EXISTS sha256          ON source TYPE string;
DEFINE FIELD IF NOT EXISTS version         ON source TYPE int    DEFAULT 1;
DEFINE FIELD IF NOT EXISTS status          ON source TYPE string DEFAULT "active"; -- active | superseded | erased
DEFINE FIELD IF NOT EXISTS custom_time     ON source TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at      ON source TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at      ON source TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_source_id     ON source FIELDS source_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_source_slug   ON source FIELDS doc_slug, owner_id;
DEFINE INDEX IF NOT EXISTS idx_source_sha256 ON source FIELDS sha256, owner_id;

-- ── 14. anchor (Deterministic Verbatim Evidence Coordinates) ──
DEFINE ANALYZER IF NOT EXISTS korda_bm25 TOKENIZERS class, blank FILTERS lowercase, snowball(english);

DEFINE TABLE IF NOT EXISTS anchor SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS anchor_id       ON anchor TYPE string;
DEFINE FIELD IF NOT EXISTS source_id       ON anchor TYPE string;
DEFINE FIELD IF NOT EXISTS doc_slug        ON anchor TYPE string;
DEFINE FIELD IF NOT EXISTS owner_id        ON anchor TYPE string;
DEFINE FIELD IF NOT EXISTS version         ON anchor TYPE int    DEFAULT 1;
DEFINE FIELD IF NOT EXISTS status          ON anchor TYPE string DEFAULT "active"; -- active | superseded
DEFINE FIELD IF NOT EXISTS quote           ON anchor TYPE string;
DEFINE FIELD IF NOT EXISTS page_num        ON anchor TYPE option<int>;
DEFINE FIELD IF NOT EXISTS section_heading ON anchor TYPE option<string>;
DEFINE FIELD IF NOT EXISTS start_sec       ON anchor TYPE option<float>;
DEFINE FIELD IF NOT EXISTS end_sec         ON anchor TYPE option<float>;
DEFINE FIELD IF NOT EXISTS figure_id       ON anchor TYPE option<string>;
DEFINE FIELD IF NOT EXISTS embedding       ON anchor TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at      ON anchor TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_anchor_id     ON anchor FIELDS anchor_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_anchor_active ON anchor FIELDS owner_id, status, doc_slug;
DEFINE INDEX IF NOT EXISTS idx_anchor_hnsw   ON anchor FIELDS embedding HNSW DIMENSION 768 TYPE F16 DIST COSINE;
DEFINE INDEX IF NOT EXISTS idx_anchor_bm25   ON anchor FIELDS quote FULLTEXT ANALYZER korda_bm25 BM25 HIGHLIGHTS;

-- Auto-supersede prior anchor versions when a newer version of the same doc_slug & owner commits.
DEFINE EVENT IF NOT EXISTS evt_anchor_supersede_v1 ON anchor WHEN $event = "CREATE" AND $after.version > 1 THEN {
    UPDATE anchor SET status = "superseded"
    WHERE doc_slug = $after.doc_slug
      AND owner_id = $after.owner_id
      AND version < $after.version
      AND status = "active";
};

-- ── 15. crystal (Verified Distilled Concept) ──────────────────
DEFINE TABLE IF NOT EXISTS crystal SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS crystal_id      ON crystal TYPE string;
DEFINE FIELD IF NOT EXISTS owner_id        ON crystal TYPE string;
DEFINE FIELD IF NOT EXISTS doc_slug        ON crystal TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS title           ON crystal TYPE string;
DEFINE FIELD IF NOT EXISTS summary         ON crystal TYPE string;
DEFINE FIELD IF NOT EXISTS status          ON crystal TYPE string DEFAULT "draft" ASSERT $value IN ["draft", "verified", "archived"];
DEFINE FIELD IF NOT EXISTS tags            ON crystal TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS embedding       ON crystal TYPE option<array<float>>;
DEFINE FIELD IF NOT EXISTS created_at      ON crystal TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at      ON crystal TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_crystal_id       ON crystal FIELDS crystal_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_crystal_verified ON crystal FIELDS owner_id, status, doc_slug;
DEFINE INDEX IF NOT EXISTS idx_crystal_hnsw     ON crystal FIELDS embedding HNSW DIMENSION 768 TYPE F16 DIST COSINE;
DEFINE INDEX IF NOT EXISTS idx_crystal_bm25     ON crystal FIELDS summary FULLTEXT ANALYZER korda_bm25 BM25 HIGHLIGHTS;

-- Provenance Graph Edges
DEFINE TABLE IF NOT EXISTS anchored_in SCHEMAFULL TYPE RELATION FROM anchor TO source;
DEFINE FIELD IF NOT EXISTS created_at ON anchored_in TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS grounded_in SCHEMAFULL TYPE RELATION FROM crystal TO anchor;
DEFINE FIELD IF NOT EXISTS confidence ON grounded_in TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS created_at ON grounded_in TYPE datetime DEFAULT time::now();


-- ============================================================
-- KORDA Lane 2: Persistent Quiz Studio & FSRS Mastery Engine
-- ============================================================

-- ── 16. exam_item (Persistent Question + Distractor Rationales)
DEFINE TABLE IF NOT EXISTS exam_item SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS item_id               ON exam_item TYPE string;
DEFINE FIELD IF NOT EXISTS owner_id              ON exam_item TYPE string;
DEFINE FIELD IF NOT EXISTS topic                 ON exam_item TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS difficulty            ON exam_item TYPE string DEFAULT "Medium" ASSERT $value IN ["Easy", "Medium", "Hard"];
DEFINE FIELD IF NOT EXISTS item_format           ON exam_item TYPE string DEFAULT "MCQ" ASSERT $value IN ["MCQ", "Multi-Select", "Short Answer", "Fill-in-the-Blank"];
DEFINE FIELD IF NOT EXISTS stem                  ON exam_item TYPE string;
DEFINE FIELD IF NOT EXISTS choices               ON exam_item TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS correct_answer        ON exam_item TYPE string | array<string>;
DEFINE FIELD IF NOT EXISTS hint                  ON exam_item TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS distractor_rationales ON exam_item TYPE option<object> FLEXIBLE DEFAULT {};
DEFINE FIELD IF NOT EXISTS anchor_ids            ON exam_item TYPE array<string> DEFAULT [];
DEFINE FIELD IF NOT EXISTS fsrs_stability        ON exam_item TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS fsrs_difficulty       ON exam_item TYPE float DEFAULT 5.0;
DEFINE FIELD IF NOT EXISTS fsrs_due_at           ON exam_item TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at            ON exam_item TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_exam_item_id    ON exam_item FIELDS item_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_exam_item_owner ON exam_item FIELDS owner_id, difficulty;
DEFINE INDEX IF NOT EXISTS idx_exam_item_due   ON exam_item FIELDS owner_id, fsrs_due_at;

-- ── 17. attempt (Mock Exam Submission & FSRS Trigger) ─────────
DEFINE TABLE IF NOT EXISTS attempt SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS attempt_id      ON attempt TYPE string;
DEFINE FIELD IF NOT EXISTS session_id      ON attempt TYPE string;
DEFINE FIELD IF NOT EXISTS item_id         ON attempt TYPE string;
DEFINE FIELD IF NOT EXISTS owner_id        ON attempt TYPE string;
DEFINE FIELD IF NOT EXISTS is_correct      ON attempt TYPE bool;
DEFINE FIELD IF NOT EXISTS rating          ON attempt TYPE int DEFAULT 3 ASSERT $value >= 1 AND $value <= 4; -- 1=Again, 2=Hard, 3=Good, 4=Easy
DEFINE FIELD IF NOT EXISTS response_ms     ON attempt TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS pre_stability   ON attempt TYPE option<float>;
DEFINE FIELD IF NOT EXISTS pre_difficulty  ON attempt TYPE option<float>;
DEFINE FIELD IF NOT EXISTS submitted_at    ON attempt TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_attempt_id           ON attempt FIELDS attempt_id UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_attempt_session      ON attempt FIELDS session_id, owner_id;
DEFINE INDEX IF NOT EXISTS idx_attempt_session_item ON attempt FIELDS session_id, item_id, owner_id UNIQUE;

-- Auto-update FSRS stability, difficulty, and next due date when an attempt is recorded or self-rated.
-- Computes from pre_stability / pre_difficulty when present so UPDATE self-ratings remain idempotent.
DEFINE EVENT IF NOT EXISTS evt_attempt_fsrs_update ON attempt WHEN $event IN ["CREATE", "UPDATE"] THEN {
    LET $item = (SELECT fsrs_stability, fsrs_difficulty FROM exam_item WHERE item_id = $after.item_id AND owner_id = $after.owner_id LIMIT 1)[0];
    IF $item != NONE {
        LET $base_stability = $after.pre_stability ?? $item.fsrs_stability;
        LET $base_difficulty = $after.pre_difficulty ?? $item.fsrs_difficulty;
        LET $passed = $after.is_correct AND $after.rating >= 2;
        LET $next_stability = IF $passed THEN
            math::min([$base_stability * (1.2 + ($after.rating * 0.35)), 365.0])
        ELSE
            math::max([$base_stability * 0.5, 0.5])
        END;
        LET $next_difficulty = IF $passed THEN
            math::max([$base_difficulty - 0.3, 1.0])
        ELSE
            math::min([$base_difficulty + 0.8, 10.0])
        END;
        LET $interval_days = math::max([<int> math::round($next_stability), 1]);
        UPDATE exam_item SET
            fsrs_stability = $next_stability,
            fsrs_difficulty = $next_difficulty,
            fsrs_due_at = time::now() + type::duration(string::concat(<string> $interval_days, "d"))
        WHERE item_id = $after.item_id AND owner_id = $after.owner_id;
    };
};

DEFINE TABLE IF NOT EXISTS tests SCHEMAFULL TYPE RELATION FROM exam_item TO crystal;
DEFINE FIELD IF NOT EXISTS created_at ON tests TYPE datetime DEFAULT time::now();

DEFINE TABLE IF NOT EXISTS attempted_on SCHEMAFULL TYPE RELATION FROM attempt TO exam_item;
DEFINE FIELD IF NOT EXISTS created_at ON attempted_on TYPE datetime DEFAULT time::now();

