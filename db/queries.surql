-- ============================================================
-- KORDA — Canonical SurrealQL Query Suite
-- Run against: --ns korda --db extractor
--
-- Covers: hybrid BM25 + HNSW retrieval (app fuses via RRF),
-- tombstone-aware reads, FSRS exam scheduling, attempt recording,
-- and NotebookLM-style Markdown export projections.
--
-- Parameters used below (bind via SDK / surrealql params):
--   $owner      string  — Supabase auth subject UUID (tenant key)
--   $embedding  array<float> — 768-dim query embedding
--   $query      string  — raw BM25 query text
--   $k          int     — candidate pool size per retriever
-- ============================================================

-- ── 1. Hybrid retrieval: vector candidates (HNSW index) ──────
-- Application performs Reciprocal Rank Fusion across this list
-- and the BM25 list below (extractor/rag.py::reciprocal_rank_fusion).
SELECT *, vector::similarity::cosine(embedding, $embedding) AS similarity
FROM anchor
WHERE owner_id = $owner
  AND status = "active"
  AND embedding != NONE
ORDER BY similarity DESC
LIMIT $k;

-- ── 2. Hybrid retrieval: BM25 candidates (FULLTEXT index) ────
SELECT *, search::score(1) AS bm25_score
FROM anchor
WHERE owner_id = $owner
  AND status = "active"
  AND quote @@ $query
ORDER BY bm25_score DESC
LIMIT $k;

-- ── 3. Reranked candidate hydration ──────────────────────────
-- After RRF picks anchor_ids, hydrate with provenance + tombstone flag.
SELECT
    anchor_id,
    quote,
    page_num,
    section_heading,
    start_sec,
    end_sec,
    figure_id,
    doc_slug,
    source.gcs_status AS media_status,   -- "active" | "purged" (tombstone)
    source.gcs_uri    AS media_uri,
    source.title      AS source_title
FROM anchor
WHERE anchor_id IN $rrf_anchor_ids
  AND owner_id = $owner;

-- ── 4. Tombstone-aware source listing ────────────────────────
-- Excludes lifecycle-purged media from live-link surfaces; the
-- anchor quotes remain citable as "tombstone" evidence cards.
SELECT source_id, doc_slug, title, mime_type, gcs_status, sha256, created_at
FROM source
WHERE owner_id = $owner
  AND status != "erased"
ORDER BY created_at DESC;

-- ── 5. FSRS due queue (mock-exam item selection) ─────────────
SELECT item_id, topic, difficulty, item_format, stem, choices, hint, anchor_ids
FROM exam_item
WHERE owner_id = $owner
  AND fsrs_due_at < time::now()
ORDER BY fsrs_due_at ASC
LIMIT 20;

-- ── 6. Record an attempt (idempotent per session+item) ───────
-- The idx_attempt_session_item UNIQUE index makes retries safe;
-- UPSERT semantics are enforced by the app layer.
UPSERT attempt CONTENT {
    attempt_id: $attempt_id,
    session_id: $session_id,
    item_id: $item_id,
    owner_id: $owner,
    is_correct: $is_correct,
    rating: $rating,
    response_ms: $response_ms,
};

-- ── 7. Session score rollup ──────────────────────────────────
-- One row per (session_id, is_correct); the app sums correct_count.
SELECT session_id, is_correct, count() AS n
FROM attempt
WHERE owner_id = $owner AND session_id = $session_id
GROUP BY session_id, is_correct;

-- ── 8a. NotebookLM-style Markdown export — anchors ───────────
-- anchor.source_id is a plain string (not a record link), so the
-- exporter joins against query 8b in application code, then renders:
--   # {source_title}
--   > {quote} — {section_heading}, p.{page_num} / {start_sec}s
SELECT
    anchor_id,
    quote,
    page_num,
    section_heading,
    start_sec,
    end_sec,
    figure_id,
    doc_slug,
    source_id
FROM anchor
WHERE owner_id = $owner
  AND status = "active"
ORDER BY doc_slug ASC, page_num ASC, start_sec ASC;

-- ── 8b. Export join side — live sources only ─────────────────
SELECT source_id, doc_slug, title, mime_type, gcs_uri
FROM source
WHERE owner_id = $owner
  AND status != "erased"
  AND gcs_status != "purged";

-- ── 9. Crystal -> evidence graph traversal ───────────────────
SELECT ->grounded_in->anchor AS evidence
FROM crystal
WHERE crystal_id = $crystal_id AND owner_id = $owner;
