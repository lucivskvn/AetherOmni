# 🚀 AetherOmni — Enterprise Multi-Model RAG & Document Intelligence Platform

> **Production-grade Django 6.x platform featuring Multi-Model LLM Gateways, Dual Database Engine (SurrealDB HNSW Vector RAG + Relational Store), Async 3-Stage Processing Pipelines, and Serverless Cloud Native Infrastructure.**

<!-- auto:badges -->
[![DevSecOps CI Pipeline](https://github.com/lucivskvn/AetherOmni/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lucivskvn/AetherOmni/actions)
[![Quality Gate Status](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![Coverage](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=coverage)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![Security Rating](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=security_rating)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![Maintainability Rating](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=sqale_rating)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![Reliability Rating](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=reliability_rating)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![Version](https://img.shields.io/badge/version-v1.5.549-blue.svg)](https://github.com/lucivskvn/AetherOmni/releases)
[![Commit](https://img.shields.io/badge/commit-ea9a355-lightgrey.svg)](https://github.com/lucivskvn/AetherOmni/commits/main)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)
<!-- /auto:badges -->

[![Desloppify Codebase Health](docs/scorecard.png)](docs/scorecard.png)

---

## 📌 Executive Summary, Technical Outputs & Business Use Cases

**AetherOmni** is an enterprise multi-lingual document intelligence and RAG platform that ingests unstructured, multi-format documents (PDF, DOCX, CSV, TXT, scanned images, and recursive ZIP archives) and transforms them into **standardized, queryable knowledge assets**.

---

## 👁️ Multi-Perspective Architectural Evaluation & Value Analysis

AetherOmni's architecture is evaluated across four primary stakeholder perspectives to articulate its concrete utility, engineering rigor, financial sustainability, and scholarly rigor.

```mermaid
flowchart TD
    Sub1["<b>🟢 Non-Technical PoV</b><br>Zero Data Entry · Automated Layout Conversion"]
    Sub2["<b>🔵 Technical & Engineering PoV</b><br>3-Stage Async Pipeline · Hybrid RAG (BM25 + HNSW)"]
    Sub3["<b>💼 Business & Enterprise PoV</b><br>SHA-256 $0.00 Caching · Immutable Audit Trail"]
    Sub4["<b>🎓 Academic Research PoV</b><br>Verifiable Citations · Multilingual SFT Datasets"]

    Sub1 --> Pipeline["<b>AetherOmni Core Engine</b>"]
    Sub2 --> Pipeline
    Sub3 --> Pipeline
    Sub4 --> Pipeline
```

---

### 1. 🟢 Non-Technical & Executive Perspective: "What Does It Do & Why Use It?"

- **The Problem**: Organizations waste thousands of hours manually copying data from PDFs, scanned contracts, images, and mixed document archives into databases and internal wikis. Crucial knowledge remains locked in silos.
- **The AetherOmni Solution**: AetherOmni acts as an **Automated Digital Knowledge Converter**. Simply upload your documents (PDFs, Word files, spreadsheets, scanned images, ZIP archives), and AetherOmni automatically cleans, organizes, transcribes, and connects your files into an intelligent, searchable library.
- **Key User Benefits**:
  - **Zero Manual Data Entry**: Reads complex tables, flowcharts, and multi-column pages automatically.
  - **Multilingual Support**: Natively handles complex languages like Arabic (with proper Right-to-Left formatting) alongside English.
  - **Instant Answers**: Ask questions in plain language and receive precise, cited answers directly referencing your uploaded documents.
  - **Clean Single-File Exports**: Download structured ZIP bundles (`documents/001_contract.md`, `manifest.json`) ready for archiving or sharing with non-technical team members.

---

### 2. 🔵 Technical & Engineering Perspective: "How Is It Built & Architected?"

- **The Pipeline Engineering**: Built on a decoupled, asynchronous 3-stage architecture (Stage 1: Layout Ingestion & SHA-256 Deduplication, Stage 2: Multi-Model LLM Gateway & Spend Control, Stage 3: SurrealDB HNSW Vector Storage & RRF RAG).
- **Hybrid Dense-Sparse RAG (Reciprocal Rank Fusion)**: Combines sparse BM25 keyword matching with dense SurrealDB HNSW vector embeddings (`DIMENSION 768 DIST COSINE`) to eliminate search hallucination and optimize context window precision.
- **Durable Tenant Ownership**: Production document access is keyed by the Supabase Auth subject UUID, so Cloud Run restarts cannot orphan a user's knowledge desk from its documents.
- **Resilient Multi-Provider Gateway**: Implements exponential backoff and circuit-breaking across stable Google Gemini 2.5 Flash / 2.5 Flash-Lite on Vertex AI, plus OpenRouter fallbacks (Llama 3, Gemma 2, Qwen 2).
- **DevSecOps & Code Health Rigor**:
  - **Shift-Left Local Verification**: Multi-language `run_checks.sh` pipeline enforcing Python AST auditing (`ruff`), static typing (`mypy`), differential security scanning (`bandit`, Semgrep, AST-Grep), JavaScript conventions (`eslint`), YAML schema validation (`yamllint`), container hardening (`hadolint`), and comprehensive automated unit test coverage. CI uses the pinned official AST-Grep CLI; the Python library distribution is not a CLI substitute. New suppressions must identify the exact rule; Semgrep and SonarQube suppressions also require a justification.
  - **Desloppify Codebase Health**: Continuous structural complexity, cohesion, and dependency cycle monitoring across all 17 sensors to maintain high objective codebase quality and security scores.
  - **Cloud SAST & Quality Gate**: Automated CI pipeline integrating static application security testing with remote SonarQube MQR Quality Gate enforcement.
  - **Immutable CI Dependencies**: GitHub Actions are pinned to reviewed commit SHAs, preventing tag-repointing supply-chain changes.
  - **Reproducible CI Tooling**: Security scanners run in an isolated environment when their dependencies differ from the application runtime, without weakening blocking checks.

---

### 3. 💼 Business & Financial Enterprise Perspective: "What Is the ROI & Governance Risk?"

- **Financial Predictability & Cost Reduction**:
  - **Instant SHA-256 Hash Caching ($0.00 Cost)**: Deduplicates incoming documents by SHA-256 checksums to instantly reuse extracted metadata without calling LLM APIs ($0.00 processing cost).
  - **Persisted Spend Accounting (`MonthlySpendLog`)**: Tracks monthly API spend in real-time. Spend logs persist even if source document records are purged, guaranteeing financial auditability.
  - **Serverless Scale-to-Zero GCP Infrastructure**: Deployed on GCP Cloud Run with zero-scale scaling limits to minimize idle infrastructure costs.
- **Regulatory Compliance & Risk Mitigation**:
  - **SOC 2 Immutable Audit Ledger**: Overridden `save()` and `delete()` methods in `AuditLog` combined with PostgreSQL database triggers and SurrealDB table permissions prevent tampering or deletion of audit logs.
  - **Data Privacy & Air-Gapped Deployment**: Supports self-hosted database execution (`SURREALDB_OFFLINE=True`) and keyless GCP Application Default Credentials (ADC) to eliminate hardcoded credentials in Git codebases.

---

### 4. 🎓 Academic & Scholarly Research Perspective: "How Does It Support Scientific Rigor?"

- **Multilingual Corpus Ingestion & Philological Preservation**: Preserves complex manuscript layouts, RTL typography, and custom metadata via standardized YAML frontmatter headers.
- **Verifiable Page-Level Grounding & Citation Attribution**: Inserts strict structural block markers (`<!-- SOURCE_START_1 -->` / `<!-- SOURCE_END_1 -->`) into `master_archival_source.md`, enabling scholars to verify AI outputs against original source pages.
- **Reproducible Dataset Creation for Machine Learning**: Automatically formats unstructured academic publications into Supervised Fine-Tuning (SFT) Q&A JSON datasets (`[{"question": "...", "answer": "..."}]`) for fine-tuning scientific domain models.

---

### 📊 Multi-Stakeholder Evaluation Summary Matrix

| Stakeholder PoV | Primary Objective | AetherOmni Feature Implementation | Practical Business & Technical Value |
| :--- | :--- | :--- | :--- |
| **Non-Technical User** | Ease of Use & Automated Ingestion | Drag-and-drop uploads, simple markdown view, instant single-copy ZIP export (`documents/001_title.md`). | Zero technical learning curve; eliminates manual document transcription. |
| **Software Engineer** | Architecture Rigor & Zero Hallucination | Decoupled 3-stage pipeline, SurrealDB HNSW vector RAG, RRF hybrid search (BM25 + HNSW). | High-precision sub-100ms retrieval with zero prompt context window waste. |
| **DevSecOps Engineer** | Security, SAST & Pipeline Stability | Complete `run_checks.sh` gate: Ruff, Mypy, ast-grep, Semgrep SAST, Bandit, Hadolint, **`surreal validate`** SurrealQL schema lint, ShellCheck, SonarQube MQR. | Prevents broken code, security vulnerabilities, or failing tests from entering main branch. |
| **CFO / Finance Lead** | Cost Control & Budget Predictability | Instant SHA-256 hash caching ($0.00 cost reuse), `MonthlySpendLog` USD caps, Cloud Run scale-to-zero. | Eliminates duplicate LLM API charges; ensures spend stays within strict monthly caps. |
| **Compliance Officer** | Auditability & SOC 2 Governance | Immutable append-only `AuditLog` with PostgreSQL triggers and client IP logging (`get_client_ip`). | Complete tamper-evident audit trail for regulatory compliance. |
| **Academic Researcher** | Scientific Rigor & Verifiable Citations | Structural source boundaries (`<!-- SOURCE_START -->`), SFT Q&A JSON dataset export, RTL Arabic layout. | Verifiable peer-reviewed citation attribution and reproducible ML dataset preparation. |

---

### 📤 Platform Technical Outputs & Output Artifact Examples

1. **Archival Structured Markdown with Metadata**:
   - Converts document layouts into clean, sanitized Markdown text preserved with YAML frontmatter headers (title, author, language, SHA-256 hash, export timestamps) and Right-to-Left (RTL) Arabic HTML wrappers (`dir="rtl" class="arabic-text"`).

   ```markdown
   ---
   title: "Enterprise Legal Contract"
   author: "Legal Compliance Team"
   language: "Arabic"
   document_type: "PDF"
   source_hash: "a591a6d40bf420404a011733cfb7b190d62c65bf0bcda32b57b277d9ad9f146e"
   exported_at: "2026-08-08T22:00:00Z"
   ---

   <div dir="rtl" class="arabic-text">
   ### اتفاقية الشروط العامة والتنفيذ
   تم الاتفاق بين الأطراف الموقعة على الالتزام الكامل بكافة بنود العقد...
   </div>
   ```

2. **Supervised Fine-Tuning (SFT) Q&A Datasets**:
   - Automatically generates structured JSON Q&A pairs for offline model fine-tuning and domain training.

   ```json
   [
     {
       "question": "How does AetherOmni ensure sub-100ms vector search latency for hybrid RAG queries?",
       "answer": "AetherOmni uses SurrealDB v3.x HNSW vector indexing combined with BM25 sparse term matching fused via Reciprocal Rank Fusion (RRF)."
     }
   ]
   ```

3. **Multi-Modal Visual Diagram & Schema Captions**:
   - Extracts flowcharts, architectural schemas, and tabular diagrams into structured Markdown text using Gemini 3.6 Vision / Vertex AI Vision.

   ```markdown
   ### 📊 Page 2 Visual Diagram & Schema Extraction
   **Diagram Type**: System Architecture Flowchart
   **Extracted Components**:
   - `Client Request` -> Dispatches PDF upload to `GCP Cloud Run`
   - `Worker Queue` -> `Cloud Tasks` enqueues ingestion job for `tasks.py`
   - `Vector Store` -> Embeddings written to `SurrealDB HNSW Index`
   ```

4. **Taxonomic Archival ZIP Bundles**:
   - Bundles document collections into structured directory trees (`Language/` and `Author/`) accompanied by `manifest.json` and a merged `master_archival_source.md`.

   ```text
   Language/
   └── arabic/
       └── 001_enterprise_legal_contract.md
   Author/
   └── legal_team/
       └── 001_enterprise_legal_contract.md
   manifest.json
   master_archival_source.md
   ```

### 🏢 Comprehensive Target Use Cases & Application Domains

AetherOmni serves three core application tiers: Business Enterprise, Academic & Scholarly Research, and AI/ML Engineering & Developer Ecosystems.

#### 1. 💼 Enterprise & Business Use Cases

- **Conversational RAG Knowledge Base**: Internal teams execute semantic search queries over processed document repositories with grounded citation attribution.
- **Legal & Compliance Archiving**: Regulatory teams export structured single-copy ZIP bundles with immutable SOC 2 audit trails (`AuditLogListView`) and spend logs (`MonthlySpendLog`).
- **Visual Diagram & Schema Analysis**: Engineering teams search and retrieve embedded architectural diagrams and flowcharts processed by multi-modal OCR.

#### 2. 🎓 Academic & Scholarly Research Use Cases

- **Multilingual Corpus Ingestion**: Digital humanists and researchers ingest multi-lingual texts (including Arabic RTL typography, ancient manuscripts, and legal codices) with structural frontmatter retention.
- **Verifiable Citation & Grounding**: Generates exact page-level and block-level citations (`<!-- SOURCE_START_1 -->`) for peer-reviewed academic synthesis.
- **Domain-Specific SFT Dataset Generation**: Formats complex academic papers into standardized JSON Q&A pairs for training specialized research models.

#### 3. 🛠️ Developer & AI Engineering Use Cases

- **Zero-Cost SHA-256 Deduplication Caching**: Developers prevent duplicate API charges during iterative dataset processing via instant SHA-256 hash lookups.
- **Multi-Provider Resilient LLM Gateway**: Fallback chain automatically switches between Gemini 3.6 Flash, Vertex AI, and OpenRouter free tiers to ensure 99.99% uptime.
- **Air-Gapped Local Verification**: Supports offline development (`SURREALDB_OFFLINE=True`) and the complete DevSecOps pipeline (`run_checks.sh`).

---

## ⚡ Current MVP Capabilities

| Feature Area | Current Production Capability | Implementation & Location |
| -------------- | ---------------- | --------------------------- |
| **Multi-Format Ingestion** | Ingests PDF, DOCX, CSV, TXT, and recursive ZIP batch archives with instant SHA-256 deduplication caching. | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) |
| **Arabic & Multilingual RTL** | Automatic Arabic typography detection (`dir="rtl" class="arabic-text"`), Markdown rendering, HTML sanitization. | `parse_arabic_layout` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L48) |
| **Multi-Model LLM Gateway** | Dynamic provider fallbacks across Gemini 3.6 Flash / 3.5 Flash-Lite, Vertex AI (multi-region), and OpenRouter (Llama 3, Gemma 2, Qwen 2 free tiers). | `generate_llm_content_unified` in [`llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) |
| **SurrealDB HNSW Vector RAG** | High-dimensional HNSW similarity search, document UUID scope filtering, Reciprocal Rank Fusion (RRF), and TTL semantic cache. | `search_rag_cache_hnsw` in [`surreal_db.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/surreal_db.py#L880) |
| **Persisted Budget Accounting** | Hard monthly USD budget caps; document deletion spend is persisted to `MonthlySpendLog`. | `MonthlySpendLog.add_cost()` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L865) |
| **Curated ZIP & Single-Copy Exports** | Single-copy standardized document exports (`documents/001_title.md`) with optional multi-taxonomy views (`Language/`, `Author/`) and `manifest.json`. | `generate_curated_zip_bundle` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L322) |
| **Automated Artifact Cleanup** | Automated DevSecOps file retention policy (`cleanup_stale_temp_artifacts`) purging temporary processing scratch files older than 24h. | `cleanup_stale_temp_artifacts` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L420) |
| **SOC 2 Immutable Audit Trail** | Logs user IDs, client IPs (`get_client_ip`), actions, and timestamps in an immutable ledger. | `AuditLogListView` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L1520) |
| **5-Phase DevSecOps Suite** | Automated verification pipeline: AST pattern scanning, Semgrep zero-finding SAST, Bandit ReDoS audit, Mypy static typing, Hadolint container hardening, **SurrealQL schema validation** (`surreal validate`), ShellCheck POSIX safety, SonarQube MQR Gatekeeper, and a comprehensive unit test suite with `coverage.xml` reporting. | `run_checks.sh`, `scripts/verify-pipeline.sh` & `.github/workflows/ci.yml` |

---

## 🗺️ Engineering Milestones & Progressive Roadmap

AetherOmni follows an **MVP-First Engineering Philosophy**, prioritizing solid core extraction, zero-cost caching, hybrid vector search, and clean batch exports before scaling to advanced multi-tenant agentic workflows.

```mermaid
flowchart LR
    M1["✅ Milestone 1.0 MVP<br>Multi-Format Ingestion & Caching"] --> M2["✅ Milestone 2.0 MVP<br>Dual DB & LLM Gateway"]
    M2 --> M3["✅ Milestone 3.0 MVP<br>Hybrid RAG & Vision OCR"]
    M3 --> M35["✅ Milestone 3.5 MVP<br>DevSecOps Hardening & SurrealQL"]
    M35 --> M36["✅ Milestone 3.6 MVP<br>Auth & Release Reliability"]
    M36 --> M37["✅ Milestone 3.7 MVP<br>Observability & Sentry Release"]
    M37 --> M4["📈 Milestone 4.0 Enterprise<br>Multi-Format Export & SQLite FTS5"]
    M4 --> M5["🚀 Milestone 5.0 Enterprise<br>Graph RAG & Agent Tools"]
```

### ✅ Milestone 1.0 (MVP Core — Multi-Format Layout Ingestion & Instant Caching)

- [x] **Multi-Format Document Ingestion**: Ingests PDF, DOCX, CSV, Excel (`.xlsx`, `.xls`), TXT, Markdown (`.md`), JSON (`.json`), and recursive ZIP batch archives with $0.00 local zero-cost parsers.
- [x] **Arabic & Multilingual RTL Typography**: Automatic Arabic layout detection (`dir="rtl" class="arabic-text"`), Markdown rendering, and HTML sanitization.
- [x] **Instant SHA-256 Hash Caching ($0.00 Cost)**: Deduplicates incoming documents by SHA-256 checksums to instantly reuse extracted metadata without calling LLM APIs.
- [x] **Standardized Batch Export & Single-Copy Bundles**: Exports single-copy standardized files (`documents/001_title.md`) with optional multi-taxonomy views (`Language/`, `Author/`), `manifest.json`, and `master_archival_source.md`.
- [x] **Automated Artifact Cleanup Policy**: Enforces DevSecOps file retention (`cleanup_stale_temp_artifacts`) to purge temporary processing scratch files older than 24 hours.

### ✅ Milestone 2.0 (MVP Core — Dual Database Engine & Multi-Model LLM Gateway)

- [x] **SurrealDB HNSW Vector Storage**: Remote SurrealDB vector indexer (`DIMENSION 768 DIST COSINE`) paired with Supabase PostgreSQL (and SQLite offline).
- [x] **Multi-Model LLM Fallback Gateway**: Dynamic provider switching across stable Gemini 2.5 Flash / 2.5 Flash-Lite (EU-first primary regions), Vertex AI, and dynamic `openrouter/free` meta-router with exponential backoff.
- [x] **Persisted Budget Accounting**: Hard monthly USD spend limits backed by immutable `MonthlySpendLog` ledgers.

### ✅ Milestone 3.0 (MVP Core — Hybrid RAG, Context Caching & Vision OCR)

- [x] **Native SurrealDB WebSocket Connection Pools**: Upgraded SurrealDB client logic for high-concurrency connection handling (`surrealdb==2.0.0`).
- [x] **Hybrid Dense-Sparse RAG Search (BM25 + HNSW)**: Implemented Reciprocal Rank Fusion (RRF) in `rag.py` to merge exact keyword BM25 matches with dense vector embeddings (`search_chunks_bm25`).
- [x] **Multi-Modal Diagram & Schema Vision OCR**: Extracted embedded flowcharts, tables, and architectural diagrams using Gemini 3.6 Vision / Vertex AI Vision (`extract_pdf_diagrams_with_vision`).
- [x] **Structural Context Chunking & Provenance Deep Linking**: Boundary-aware chunking preserving Surahs, Ayahs, and Hadiths with page and chapter metadata (`page_number`, `chapter_title`, `anchor_id`) stored in SurrealDB `chunks`.
- [x] **SurrealDB Context Caching & Memories**: Zero-cost query short-circuiting via `rag_cache` (cosine distance $\le 0.15$), tokenized `context_cache`, and `user_memories`.

### ✅ Milestone 3.5 (MVP Core — DevSecOps Hardening, SurrealQL Validation & Runtime Alignment)

- [x] **Runtime Upgrade**: Builder and runtime use the digest-pinned image declared by `Dockerfile`; CI and Ruff follow the canonical versions in project configuration.
- [x] **Shell-Free Container Startup**: A Python entrypoint runs migrations, starts bounded database initialization, and `exec`s Gunicorn without a shell interpreter in the startup path.
- [x] **SurrealQL Schema Validation** (`surreal validate`): `schema.surql` is validated on every pipeline run via the official `surreal` CLI. Integrated into Phase 2 of `run_checks.sh` and the fast differential `--fast` pass for `.surql` file changes.
- [x] **Full-Suite Tool Alignment**: All DevSecOps tools verified at latest stable — `ruff`, `mypy`, `bandit`, `pip-audit`, `semgrep`, `yamllint`, `hadolint`, `ast-grep`, `markdownlint-cli`, `eslint`, `shellcheck`, `surreal`. Application dependencies are tracked in `requirements.txt`; local Python verification tools are tracked in `requirements-dev.txt`.
- [x] **SonarQube Multi-Language SAST**: Removed the single-language lock; SonarQube scans Python and JavaScript in the same analysis pass using repository-managed analyzer configuration.
- [x] **Python Runtime Alignment**: Docker, GitHub Actions, local checks, and SonarQube use Python 3.14 semantics, preventing version-dependent findings and syntax drift.
- [x] **Full Test Suite**: All Django unit tests pass cleanly under `SURREALDB_OFFLINE=True` with `coverage.xml` generated for SonarQube ingestion.

### ✅ Milestone 3.6 (MVP Reliability — Authentication & Release Integrity)

- [x] **Supabase Email Login Recovery**: Turnstile is required before credential dispatch and its token is forwarded through GoTrue security metadata; successful sessions bridge into Django without first-user privilege escalation. GitHub OAuth and Passkeys remain planned.
- [x] **Release Traceability**: SonarQube and Cloud Build derive the same commit-count release from full Git history, then propagate it to the immutable image tag, Cloud Run, and application UI. Cloud Build waits for the exact commit's successful mainline SonarQube check before deployment.
- [x] **Worker-Only Ingestion Dispatch**: Production uploads enqueue OIDC-authenticated work for the worker service only. Cloud Build resolves worker routing and the Vertex project identity at deploy time; an optional public-origin substitution keeps Supabase confirmation redirects on the browser-facing application URL.
- [x] **On-Demand Worker Processing**: Cloud Tasks wakes a bounded zero-minimum worker only for queued ingestion; periodic maintenance is disabled by default and can be enabled only with an explicit always-on operating decision. Spend-ledger persistence is validated before document deletion.
- [ ] **Protected Delivery Path**: Require PR checks for DevSecOps, CodeQL, dependency review, and SonarQube before `main` can merge.

### ✅ Milestone 3.7 (MVP Reliability — Operations & Multi-MCP Triage)

- [x] **Multi-MCP Triage & Observability**: Dedicated Model Context Protocol server workflows for SonarQube quality gates, Google Cloud Logging container inspections, Google Cloud Monitoring metrics, Chrome DevTools accessibility testing, and Google Developer Knowledge.
- [x] **Operational Runbook & Diagnostic Tools**: Read-only GCP diagnostics CLI (`scripts/gcp-diagnostics.sh`) for Cloud Run revisions, readiness status, and bounded error log inspection.
- [x] **Sentry Release Observability**: Correlated errors, performance tracing, profiling, and deployments with computed `RELEASE_VERSION` and verification test route (`/sentry-debug/`).
- [ ] **Pulumi Foundation**: Model and import Cloud Run, Secret Manager, IAM, Artifact Registry, Cloud Tasks, and Storage before provisioning another environment.

### 📦 Milestone 4.0 (Enterprise Roadmap — Multi-Format Export, Real-Time Streaming & RBAC)

- [x] **Full Legal & Copyright Metadata Extraction**: Embeds Publisher, Publication Year, License Type (CC-BY-4.0, MIT), DOI, SHA-256 hash, and `validation_status` across schemas, models, and export headers.
- [x] **User Provenance & Authentication Tracking**: Embeds `uploaded_by_user_id`, `uploaded_by_username`, and `exported_by_username` in exported headers and manifest metadata.
- [x] **Multi-Format Export Selector**: Download extracted datasets in **Markdown (`.zip`)**, **Hugging Face SFT (`.jsonl`)**, **SQLite Mobile (`.db`)**, and **CSV Summary (`.csv`)** with formula injection sanitization.
- [x] **Offline Mobile SQLite FTS5 Indexing**: Self-contained SQLite `.db` bundles with FTS5 full-text search index for offline iOS / Android / Flutter integration.
- [ ] **Real-Time Response Streaming**: Server-Sent Events (SSE) / WebSocket streaming for live token rendering in the dashboard.
- [ ] **Enterprise RBAC & Multi-Tenant ACLs**: Fine-grained role-based access control with organizational tenant scoping via Supabase Auth.
- [ ] **Automated RAG Benchmarking**: Continuous assessment of context precision, answer relevance, and faithfulness via RAGAS and TruLens.

### 🚀 Milestone 5.0 (Enterprise Roadmap — Graph RAG & Autonomous Agent Tools)

- [ ] **Multi-Tenant Knowledge Graph RAG**: SurrealDB Graph Relational RAG linking entities, concepts, and document nodes.
- [ ] **Autonomous Tool-Executing Agents**: Integration with Google Antigravity Agentic SDK for automated multi-step workflow execution.

---

## 🏗️ 3-Stage Architectural Pipeline

```text
┌─────────────────────────┐     ┌─────────────────────────┐     ┌─────────────────────────┐
│     STAGE 1: LAYOUT     │ ──> │   STAGE 2: REFINEMENT   │ ──> │     STAGE 3: VECTOR     │
│   Ingestion & Parsing   │     │    Multi-Model LLM     │     │  SurrealDB HNSW Index  │
└─────────────────────────┘     └─────────────────────────┘     └─────────────────────────┘
```

| Pipeline Stage | Implementation Module | Architecture & Operations |
| ---------------- | ----------------- | ----------------------------------- |
| **Stage 1: Layout & Ingestion** | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) | • Validates document headers, sanitizes HTML, computes SHA-256 hashes.<br>• Executes instant SHA-256 hash deduplication ($0.00 cost reuse).<br>• Parses Arabic RTL typography (`parse_arabic_layout`) and extracts YAML frontmatter.<br>• Unpacks ZIP archives recursively into single-copy standardized files (`documents/001_title.md`). |
| **Stage 2: LLM Refinement & Cost Control** | [`extractor/llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) | • Evaluates `check_budget_and_api_limit()` against `MonthlySpendLog` USD caps.<br>• Dispatches prompts across primary LLM providers (Gemini / Vertex / OpenRouter) with exponential backoff.<br>• Extracts multi-modal visual diagrams and flowcharts via Gemini 3.6 Vision.<br>• Calculates real-time prompt/completion token spend and logs costs. |
| **Stage 3: Vector HNSW Indexing & RAG** | [`extractor/rag.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/rag.py) | • Executes semantic boundary chunking (`chunk_document_semantically`).<br>• Generates text embeddings and writes to SurrealDB HNSW vector index (`DIMENSION 768`).<br>• Executes Reciprocal Rank Fusion (RRF) combining BM25 keyword matching with dense HNSW vector search.<br>• Manages TTL-enforced RAG cache (`upsert_rag_cache`) for fast semantic retrieval. |

---

## 🧰 Technology Stack Inventory

| Component Layer | Technology / Tool | Version / Details | Purpose |
| ----------------- | ------------------- | ------------------- | --------- |
| **Core Framework** | Python / Django | Python 3.14 · Django 5.x | Core MVC architecture, ORM data layer, admin backend, session management |
| **Relational Storage & Auth** | Supabase PostgreSQL / Supabase Auth | PostgreSQL 17 · GoTrue REST API · Cloudflare Turnstile | User identity, authentication, session tokens with `gotrue_meta_security`, spend logs, and audit trails |
| **High-Throughput Vector & Cache DB** | SurrealDB | v3.x (HNSW Indexing) · SDK `surrealdb==2.0.0` | Multi-model vector store (HNSW 768 cosine), prompt prefix cache (`context_cache`), sliding rate limits (`rate_limits`), and `user_memories` |
| **Secrets & Keyless IAM** | GCP Secret Manager / IAM ADC | Application Default Credentials (ADC) | Keyless IAM runtime authentication, dynamic resolution of API keys (`OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `SUPABASE_SERVICE_ROLE_KEY`) with zero committed secrets |
| **LLM Gateway & Multimodal AI** | Google Gemini / Vertex AI / OpenRouter | Gemini 2.5 Flash / 2.5 Flash-Lite (EU-first primary), OpenRouter dynamic `openrouter/free` | Multi-provider fallback chain with cost control, multi-modal diagram extraction, and automated retries |
| **Cloud Serverless Hosting** | GCP Cloud Run | Fully Managed Serverless · region `asia-southeast1` | Zero-scale web app and worker process containers with ephemeral stateless persistence |
| **Asynchronous Task Queue** | GCP Cloud Tasks | OIDC Authenticated Worker Tasks | Production asynchronous document processing queue with localized thread fallbacks |
| **Cloud Object Storage** | Google Cloud Storage | GCS Bucket (`google-cloud-storage`) | Secure cloud asset storage for raw documents and curated export bundles |
| **CI/CD & Git Automation** | GitHub Actions / GitHub CLI | Pinned commit SHAs · `gh` CLI | 3-Phase Shift-Left validation, CodeQL, and Dependabot security |
| **Container Runtime & Build** | Docker / Kaniko | Multi-stage OWASP non-root build · Kaniko debug image | Immutable digest-pinned containers with zero shell footprint in the application startup path |
| **DevSecOps & SAST Suite** | SonarCloud / Semgrep Cloud SAST / Hadolint / Ruff / Mypy / ast-grep / **surreal validate** / Desloppify | SonarCloud Quality Gate, Semgrep SAST, SurrealQL validation | Shift-left security verification, static typing, regex ReDoS prevention, and continuous codebase health |
| **AI Agent Tooling & MCP** | Sequential Thinking / SonarCloud / Google Cloud Logging / Chrome DevTools / Google Dev Knowledge | Model Context Protocol (MCP) servers | Fast grounded triage, multi-step sequential reasoning, live quality gate queries, Cloud Run log inspection, and UI/UX accessibility auditing |

---

## ✨ Core Feature Matrix

- 🌐 **Multilingual & RTL Layout Preservation**: Full support for Right-to-Left Arabic text formatting and multi-column document parsing.
- 📦 **Curated ZIP Archival Export**: Bundles filtered documents into organized folder hierarchies (`Language/English`, `Author/Shakespeare`) complete with `manifest.json` and combined `master_archival_source.md`.
- 🔎 **Hybrid Semantic RAG Search**: Combines SurrealDB HNSW vector search with BM25 sparse keyword matching using Reciprocal Rank Fusion (RRF).
- 💰 **Persisted Monthly Spend Accounting**: Persists deleted document costs via `MonthlySpendLog.add_cost()` to maintain financial audit integrity even after purging records.
- 🛡️ **SOC 2 & ISO 27001 Audit Logs**: Logs client IP addresses (`get_client_ip`), user IDs, action names, and timestamps in an immutable audit trail (`AuditLogListView`).

---

## 🛡️ DevSecOps & 5-Phase Quality Gates

AetherOmni strictly enforces **Shift-Left Local Verification** before code can be committed or merged into production branches.

### 🧪 Complete Verification Gate (`run_checks.sh`)

Execute the local verification script to validate all quality gates prior to opening a Pull Request. Pipeline failure propagation ensures captured test output cannot turn a failed command into a false-green result:

```bash
bash run_checks.sh --autofix
# OR the full pipeline (includes git pull, Desloppify, and SonarCloud submission):
bash scripts/verify-pipeline.sh
```

`run_checks.sh` executes the complete quality gate pipeline in sequence:

1. **Phase 1: Code Formatting & Syntax**:
   - Ruff AST Formatter (`ruff format --check .`)
   - Ruff Cyclomatic Complexity & Linter (`ruff check .`)
   - Yamllint Configuration Audit & Hadolint Docker Hardening
2. **Phase 2: Infrastructure & Schema Validation**:
   - Mypy Data Flow & Static Type Checker (`mypy core/ extractor/`)
   - AST-Grep Structural Pattern Auditor (`ast-grep scan`)
   - **SurrealQL Schema Validator** (`surreal validate **/*.surql`) — enforces SurrealQL syntax on all `.surql` files
   - ShellCheck POSIX Script Safety Auditor (`shellcheck run_checks.sh scripts/*.sh`)
3. **Phase 3: Deep Security & SAST Audit**:
   - Semgrep OSS SAST Engine (`semgrep scan --config=auto`) — strict 0 findings gate
   - Bandit ReDoS & Cryptographic Vulnerability Auditor (`bandit -c bandit.yaml`)
   - Pip-Audit Supply-Chain CVE Dependency Audit (`pip-audit -r requirements.txt`)
4. **Phase 4: Runtime Verification & Test Suite**:
   - Django System Integrity Check (`python manage.py check`)
   - Django Unit Test Suite & Coverage Export (`coverage run manage.py test` & `coverage.xml`)
5. **Phase 5: Documentation Governance & SonarCloud Gate**:
   - Markdownlint Syntax Auditor (`markdownlint README.md docs/gcp_deployment_guide.md`)
   - Automated Version & Metadata Synchronizer (`python scripts/update_docs.py`)
   - GitHub workflow updates are manual-only to avoid auto-commits on `main`
     that can create merge/rebase churn.

### 🔒 Remote 3-Phase CI/CD Pipeline

Every commit pushed to GitHub automatically triggers the remote CI/CD workflow (`.github/workflows/ci.yml`):

1. **Pre-Scan Validation**: Blocks on shell-script or container-file lint failures (`hadolint`).
2. **SonarCloud Deep SAST & Multi-Language Quality Gate**: Runs Ruff, ESLint, Python coverage tests (`coverage.xml`), JavaScript coverage tests (Vitest LCOV), and SonarCloud code analysis, alongside a separate parallel Semgrep Cloud SAST job across both pull requests and mainline pushes with native GitHub PR annotations.
3. **Quality Gate Gatekeeper**: Publishes the actionable condition table to the Actions log and summary, annotates failing metrics, and blocks failed gates. Cloud Build independently waits for that exact commit check before mutating Cloud Run.

Cloud Build uses Kaniko's BusyBox-enabled debug image, pinned by immutable
digest, when a build step must source computed release metadata. The standard
executor image is shell-less. It uses a registry-backed cache and bounded pull,
extraction, and push retries.

---

## 🚀 Local Development Setup Guide

### 1. Prerequisites

- The Python runtime declared by `pyproject.toml` installed
- Docker & Docker Compose (optional for SurrealDB)

### 2. Environment Configuration

Create a `.env` file in the root directory:

```env
DJANGO_SECRET_KEY="your-secure-development-secret-key"
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
GEMINI_API_KEY="your-gemini-api-key"
SURREAL_URL="ws://localhost:8001/rpc"
SURREAL_USER="root"
SURREAL_PASS="root"
SURREALDB_OFFLINE=False
```

> Note: production and remote SurrealDB connections must use a WebSocket RPC URL configured via `SURREAL_URL` (e.g. `wss://<surrealdb-host>/rpc`).

### 3. Install Dependencies & Initialize Database

```bash
# Recommended: use uv (fast Python package manager)
# Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv venv .venv
source .venv/bin/activate

# Install requirements
uv pip install -r requirements-dev.txt

# Run migrations & initialize SurrealDB
python manage.py migrate
python scripts/init_surreal.py
```

> **Alternative (standard venv):** create `.venv` with the interpreter required by `pyproject.toml`, activate it, and install `requirements-dev.txt`.

### 4. Launch Development Server

```bash
python manage.py runserver 0.0.0.0:8000
```

Access the application at `http://localhost:8000`.

### 5. Execute Test Suite

```bash
DJANGO_SECRET_KEY=test_key SECURE_SSL_REDIRECT=False python manage.py test extractor.tests
```

---

## ☁️ Cloud Run Deployment & Live Diagnostics

### Deploying to GCP Cloud Run

Refer to the complete deployment guide in [`gcp_deployment_guide.md`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/gcp_deployment_guide.md).

### Live Cloud Diagnostics

To inspect readiness, recent revisions, and recent runtime errors without reading
secrets or changing cloud resources:

```bash
bash scripts/gcp-diagnostics.sh --service all
```

---

## 📄 License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**. See [`LICENSE`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/LICENSE) for full details.
