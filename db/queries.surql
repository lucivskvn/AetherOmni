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
-- Uses the KNN operator so idx_anchor_hnsw is engaged (a plain
-- cosine ORDER BY would full-scan). The app widens the pool to
-- min(k*3, 100) before RRF; tune the literal ef/pool here.
SELECT anchor_id, source_id, doc_slug, quote, page_num, section_heading,
       start_sec, end_sec, figure_id,
       vector::distance::knn() AS dist
FROM anchor
WHERE owner_id = $owner
  AND status = "active"
  AND embedding != NONE
  AND embedding <|60,40|> $embedding
ORDER BY dist ASC
LIMIT $k;

-- ── 2. Hybrid retrieval: BM25 candidates (FULLTEXT index) ────
-- @1@ binds the match predicate to highlight ref 1 so
-- search::score(1)/search::highlight(...,1) resolve correctly.
SELECT anchor_id, source_id, doc_slug, quote, page_num, section_heading,
       start_sec, end_sec, figure_id,
       search::score(1) AS bm25_score,
       search::highlight('**', '**', 1) AS highlighted_quote
FROM anchor
WHERE owner_id = $owner
  AND status = "active"
  AND quote @1@ $query
ORDER BY bm25_score DESC
LIMIT $k;

-- ── 3. Reranked candidate hydration ──────────────────────────
-- anchor.source_id is a plain string (not a record link), so source
-- fields are NOT traversable here — join via query 3b in app code.
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
WHERE anchor_id IN $rrf_anchor_ids
  AND owner_id = $owner;

-- ── 3b. Source join for query 3 — live + tombstone media state ─
SELECT source_id, doc_slug, title, mime_type, gcs_uri, gcs_status, sha256
FROM source
WHERE source_id IN $source_ids
  AND owner_id = $owner
  AND status != "erased";

-- ── 4. Tombstone-aware source listing ────────────────────────
-- Excludes lifecycle-purged media from live-link surfaces; the
-- anchor quotes remain citable as "tombstone" evidence cards.
SELECT source_id, doc_slug, title, mime_type, gcs_status, sha256, created_at
FROM source
WHERE owner_id = $owner
  AND status != "erased"
ORDER BY created_at DESC;

-- ── 5. FSRS due queue (mock-exam item selection) ─────────────
-- correct_answer is deliberately NOT selected: answers stay
-- server-gated until the attempt is graded/revealed.
SELECT item_id, topic, difficulty, item_format, stem, choices, hint, anchor_ids
FROM exam_item
WHERE owner_id = $owner
  AND fsrs_due_at < time::now()
ORDER BY fsrs_due_at ASC
LIMIT 20;

-- ── 6. Record an attempt (idempotent per session+item) ───────
-- Mirrors extractor/surreal_db.py: guard on the UNIQUE
-- (session_id, item_id, owner_id) index inside a transaction so
-- retries are no-ops instead of constraint violations. The
-- evt_attempt_fsrs_update event reschedules the exam_item.
BEGIN TRANSACTION;
LET $owned_item = (SELECT fsrs_stability, fsrs_difficulty FROM exam_item
    WHERE item_id = $item_id AND owner_id = $owner LIMIT 1)[0];
LET $existing = (SELECT VALUE attempt_id FROM attempt
    WHERE session_id = $session_id AND item_id = $item_id
      AND owner_id = $owner LIMIT 1)[0];
IF $owned_item != NONE AND $existing == NONE {
    LET $a = type::record("attempt", $attempt_id);
    LET $e = type::record("exam_item", $item_id);
    CREATE $a CONTENT {
        attempt_id: $attempt_id,
        session_id: $session_id,
        item_id: $item_id,
        owner_id: $owner,
        is_correct: $is_correct,
        rating: $rating,
        response_ms: $response_ms,
        pre_stability: $owned_item.fsrs_stability,
        pre_difficulty: $owned_item.fsrs_difficulty,
    };
    RELATE $a->attempted_on->$e;
};
COMMIT TRANSACTION;

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
-- Matches extractor/surreal_db.py: returns supporting anchor_ids.
SELECT ->grounded_in->anchor.anchor_id AS anchor_ids
FROM crystal
WHERE crystal_id = $crystal_id AND owner_id = $owner;
