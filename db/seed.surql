-- ============================================================
-- KORDA — SurrealDB Seed Data (dev/test only)
-- Apply AFTER schema.surql:
--   surreal import --conn $SURREAL_URL --user <user> --pass <pass> \
--     --ns korda --db extractor db/seed.surql
--
-- Covers the spec surface: media provenance (pdf page + video
-- timestamps), typed RELATE edges, FSRS exam items, and attempts.
-- Embeddings are intentionally omitted (option fields) — run the
-- ingestion pipeline to populate real vectors.
-- ============================================================

LET $owner = "00000000-0000-4000-8000-0000000000aa";

-- ── Sources (media provenance) ──────────────────────────────
LET $src_pdf = (CREATE source CONTENT {
    source_id: "seed-src-calculus-1",
    doc_slug: "calculus-early-transcendentals",
    owner_id: $owner,
    title: "Calculus: Early Transcendentals",
    mime_type: "application/pdf",
    gcs_uri: "gs://korda-media/raw/ephemeral/demo/calculus-early-transcendentals/sample.pdf",
    gcs_status: "active",
    sha256: "seed-sha256-pdf-0001",
    version: 1,
    status: "active",
}).id;

LET $src_video = (CREATE source CONTENT {
    source_id: "seed-src-lecture-1",
    doc_slug: "lecture-03-integrals",
    owner_id: $owner,
    title: "Lecture 03 — Integration Techniques",
    mime_type: "video/mp4",
    gcs_uri: "gs://korda-media/raw/ephemeral/demo/lecture-03/sample.mp4",
    gcs_status: "active",
    sha256: "seed-sha256-video-0001",
    version: 1,
    status: "active",
}).id;

-- ── Anchors: verbatim quotes with page/slide/video provenance ──
LET $a_pdf = (CREATE anchor CONTENT {
    anchor_id: "seed-anc-pdf-limit-1",
    source_id: "seed-src-calculus-1",
    doc_slug: "calculus-early-transcendentals",
    owner_id: $owner,
    version: 1,
    status: "active",
    quote: "The limit of f(x) as x approaches a is the value that f(x) approaches as x gets closer and closer to a.",
    page_num: 77,
    section_heading: "1.2 The Limit of a Function",
}).id;

LET $a_pdf2 = (CREATE anchor CONTENT {
    anchor_id: "seed-anc-pdf-deriv-1",
    source_id: "seed-src-calculus-1",
    doc_slug: "calculus-early-transcendentals",
    owner_id: $owner,
    version: 1,
    status: "active",
    quote: "The derivative of a function f at a is the limit of the difference quotient as h approaches 0.",
    page_num: 112,
    section_heading: "2.1 Derivatives",
}).id;

LET $a_vid = (CREATE anchor CONTENT {
    anchor_id: "seed-anc-vid-u-sub-1",
    source_id: "seed-src-lecture-1",
    doc_slug: "lecture-03-integrals",
    owner_id: $owner,
    version: 1,
    status: "active",
    quote: "For u-substitution, pick u so that du appears in the integrand up to a constant factor.",
    start_sec: 342.5,
    end_sec: 351.0,
    section_heading: "Integration by Substitution",
}).id;

-- ── Typed RELATE edges: anchor -> source ────────────────────
RELATE $a_pdf  ->anchored_in->$src_pdf;
RELATE $a_pdf2 ->anchored_in->$src_pdf;
RELATE $a_vid  ->anchored_in->$src_video;

-- ── Crystal (synthesized note) grounded in anchors ──────────
LET $crystal = (CREATE crystal CONTENT {
    crystal_id: "seed-cry-limits-derivatives",
    owner_id: $owner,
    doc_slug: "calculus-early-transcendentals",
    title: "Limits and Derivatives",
    summary: "Limits define continuity; derivatives are limits of difference quotients. Both rely on the epsilon-delta intuition of approach.",
    status: "verified",
    tags: ["calculus", "limits", "derivatives"],
}).id;

RELATE $crystal->grounded_in->$a_pdf  SET confidence = 0.95;
RELATE $crystal->grounded_in->$a_pdf2 SET confidence = 0.90;

-- ── Concept entity + edge (knowledge graph) ─────────────────
LET $concept = (CREATE entities CONTENT {
    name: "Limit (calculus)",
    category: "CONCEPT",
    description: "The value a function approaches as its input approaches a point.",
    tenant_id: $owner,
}).id;

RELATE $a_pdf->chunk_references->$concept SET relevance_score = 0.9;

-- ── Exam items (FSRS-scheduled) + attempts ──────────────────
LET $item1 = (CREATE exam_item CONTENT {
    item_id: "seed-item-limit-1",
    owner_id: $owner,
    topic: "Limits",
    difficulty: "Medium",
    item_format: "MCQ",
    stem: "What does lim(x->a) f(x) describe?",
    choices: [
        "The value f(a) itself",
        "The value f(x) approaches as x gets close to a",
        "The largest value of f on [a-1, a+1]",
        "The slope of the tangent at a",
    ],
    correct_answer: "The value f(x) approaches as x gets close to a",
    hint: "Think about 'approaches', not 'equals'.",
    anchor_ids: ["seed-anc-pdf-limit-1"],
}).id;

LET $item2 = (CREATE exam_item CONTENT {
    item_id: "seed-item-usub-1",
    owner_id: $owner,
    topic: "Integration",
    difficulty: "Hard",
    item_format: "MCQ",
    stem: "In u-substitution for ∫f(g(x))·g'(x)dx, what should u be?",
    choices: [
        "x itself",
        "g(x), so that du = g'(x)dx",
        "f(g(x))",
        "The constant of integration",
    ],
    correct_answer: "g(x), so that du = g'(x)dx",
    anchor_ids: ["seed-anc-vid-u-sub-1"],
}).id;

LET $attempt = (CREATE attempt CONTENT {
    attempt_id: "seed-att-0001",
    session_id: "seed-session-0001",
    item_id: "seed-item-limit-1",
    owner_id: $owner,
    is_correct: true,
    rating: 3,
    response_ms: 8400,
}).id;

-- exam_item -> crystal (tests), attempt -> exam_item (attempted_on)
RELATE $item1->tests->$crystal;
RELATE $item2->tests->$crystal;
RELATE $attempt->attempted_on->$item1;

SELECT count() FROM source GROUP ALL;
SELECT count() FROM anchor GROUP ALL;
SELECT count() FROM crystal GROUP ALL;
SELECT count() FROM exam_item GROUP ALL;
SELECT count() FROM attempt GROUP ALL;
