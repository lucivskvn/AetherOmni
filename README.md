# 🚀 AetherOmni — Enterprise Multi-Model RAG & Document Intelligence Platform

> **Production-grade Django 6.x platform featuring Multi-Model LLM Gateways, Dual Database Engine (SurrealDB HNSW Vector RAG + Relational Store), Async 3-Stage Processing Pipelines, and Serverless Cloud Native Infrastructure.**

[![DevSecOps CI Pipeline](https://github.com/lucivskvn/AetherOmni/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lucivskvn/AetherOmni/actions)
[![Python Version](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Django Version](https://img.shields.io/badge/Django-6.0%2B-092E20.svg)](https://www.djangoproject.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

[![Quality gate status](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=alert_status&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)
[![Security Hotspots](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=security_hotspots&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)
[![Reliability Issues](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=software_quality_reliability_issues&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)
[![Maintainability Issues](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=software_quality_maintainability_issues&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)
[![Coverage](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=coverage&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)
[![Lines of Code](https://sonarqube.fainko.cloud/api/project_badges/measure?project=aetheromni&metric=ncloc&token=sqb_e8d39ff98f4683653935932492f1aa23013f1c0e)](https://sonarqube.fainko.cloud/dashboard?id=aetheromni)

---

## 📌 Executive Summary, Technical Outputs & Business Use Cases

**AetherOmni** is an enterprise multi-lingual document intelligence and RAG platform that ingests unstructured, multi-format documents (PDF, DOCX, CSV, TXT, scanned images, and recursive ZIP archives) and transforms them into **standardized, queryable knowledge assets**.

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

### 🏢 Business Use Cases & Output Consumption

- **Conversational Enterprise RAG Knowledge Base**: Internal teams execute semantic search queries over processed document repositories with grounded citation attribution.
- **Custom LLM Fine-Tuning Pipeline**: ML teams consume generated JSON Q&A datasets to fine-tune domain-specific models.
- **Legal & Compliance Archiving**: Regulatory teams export structured ZIP bundles with immutable SOC 2 audit trails (`AuditLogListView`) and spend logs (`MonthlySpendLog`).
- **Visual Diagram & Schema Analysis**: Engineering teams search and retrieve embedded architectural diagrams and flowcharts processed by multi-modal OCR.

---

## ⚡ Current Functional Capabilities (Current State v1.2.364)

| Feature Area | Current Production Capability | Implementation & Location |
| -------------- | ---------------- | --------------------------- |
| **Multi-Format Ingestion** | Ingests PDF, DOCX, CSV, TXT, and recursive ZIP batch archives. | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) |
| **Arabic & Multilingual RTL** | Automatic Arabic typography detection (`dir="rtl" class="arabic-text"`), Markdown rendering, HTML sanitization. | `parse_arabic_layout` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L48) |
| **Multi-Model LLM Gateway** | Dynamic provider fallbacks across Gemini 3.6 Flash / 3.5 Flash-Lite, Vertex AI (multi-region), and OpenRouter (Llama 3, Gemma 2, Qwen 2 free tiers). | `generate_llm_content_unified` in [`llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) |
| **SurrealDB HNSW Vector RAG** | High-dimensional HNSW similarity search, document UUID scope filtering, and TTL semantic cache. | `search_rag_cache_hnsw` in [`surreal_db.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/surreal_db.py#L880) |
| **Persisted Budget Accounting** | Hard monthly USD budget caps; document deletion spend is persisted to `MonthlySpendLog`. | `MonthlySpendLog.add_cost()` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L865) |
| **Curated ZIP Bundling** | Filtered document subset exports organized into `Language/` and `Author/` taxonomies with `manifest.json`. | `generate_curated_zip_bundle` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L322) |
| **SOC 2 Immutable Audit Trail** | Logs user IDs, client IPs (`get_client_ip`), actions, and timestamps in an immutable ledger. | `AuditLogListView` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L1520) |
| **5-Phase DevSecOps Suite** | Automated verification pipeline featuring AST pattern scanning, Semgrep zero-finding SAST, Bandit ReDoS audit, Mypy typing, Hadolint container hardening, SonarQube MQR Gatekeeper, and unit test suite with coverage reporting. | `run_checks.sh`, `scripts/verify-pipeline.sh` & `.github/workflows/ci.yml` |

---

## 🗺️ Engineering Milestones & Progressive Roadmap

AetherOmni follows an **MVP-First Engineering Philosophy**, prioritizing solid core extraction, zero-cost caching, hybrid vector search, and clean batch exports before scaling to advanced multi-tenant agentic workflows.

```mermaid
flowchart LR
    M1["Milestone 1.0 MVP<br>Multi-Format Ingestion & Caching"] --> M2["Milestone 2.0 MVP<br>Dual DB & LLM Gateway"]
    M2 --> M3["Milestone 3.0 MVP<br>Hybrid RAG & Vision OCR"]
    M3 --> M4["Milestone 4.0 Enterprise<br>SSE Streaming & RBAC"]
    M4 --> M5["Milestone 5.0 Enterprise<br>Graph RAG & Agent Tools"]
```

### ✅ Milestone 1.0 (MVP Core — Multi-Format Layout Ingestion & Instant Caching)

- [x] **Multi-Format Document Ingestion**: Ingests PDF, DOCX, CSV, TXT, and recursive ZIP batch archives.
- [x] **Arabic & Multilingual RTL Typography**: Automatic Arabic layout detection (`dir="rtl" class="arabic-text"`), Markdown rendering, and HTML sanitization.
- [x] **Instant SHA-256 Hash Caching ($0.00 Cost)**: Deduplicates incoming documents by SHA-256 checksums to instantly reuse extracted metadata without calling LLM APIs.
- [x] **Standardized Batch Export & Single-Copy Bundles**: Exports single-copy standardized files (`documents/001_title.md`) with optional multi-taxonomy views (`Language/`, `Author/`), `manifest.json`, and `master_archival_source.md`.
- [x] **Automated Artifact Cleanup Policy**: Enforces DevSecOps file retention (`cleanup_stale_temp_artifacts`) to purge temporary processing scratch files older than 24 hours.

### ✅ Milestone 2.0 (MVP Core — Dual Database Engine & Multi-Model LLM Gateway)

- [x] **SurrealDB HNSW Vector Storage**: SurrealDB vector indexer (`DIMENSION 768 DIST COSINE`) paired with relational SQLite metadata store.
- [x] **Multi-Model LLM Fallback Gateway**: Dynamic provider switching across Gemini 3.6 Flash / 3.5 Flash-Lite, Vertex AI (multi-region), and OpenRouter free tiers with exponential backoff.
- [x] **Persisted Budget Accounting**: Hard monthly USD spend limits backed by immutable `MonthlySpendLog` ledgers.

### ✅ Milestone 3.0 (MVP Core — Hybrid RAG & Multi-Modal Vision OCR)

- [x] **Native SurrealDB WebSocket Connection Pools**: Upgraded SurrealDB client logic for high-concurrency connection handling (`surrealdb-python`).
- [x] **Hybrid Dense-Sparse RAG Search (BM25 + HNSW)**: Implemented Reciprocal Rank Fusion (RRF) in `rag.py` to merge exact keyword BM25 matches with dense vector embeddings (`search_chunks_bm25`).
- [x] **Multi-Modal Diagram & Schema Vision OCR**: Extracted embedded flowcharts, tables, and architectural diagrams using Gemini 3.6 Vision / Vertex AI Vision (`extract_pdf_diagrams_with_vision`).

### 🎯 Milestone 4.0 (Enterprise Roadmap — Real-Time Streaming & Access Control)

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
| **Stage 1: Layout & Ingestion** | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) | • Validates document headers, sanitizes HTML, computes SHA-256 hashes.<br>• Parses Arabic RTL typography (`parse_arabic_layout`) and extracts YAML frontmatter.<br>• Unpacks ZIP archives recursively into structured folder taxonomies (`Language/`, `Author/`). |
| **Stage 2: LLM Refinement & Cost Control** | [`extractor/llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) | • Evaluates `check_budget_and_api_limit()` against `MonthlySpendLog` USD caps.<br>• Dispatches prompts across primary LLM providers with exponential backoff.<br>• Calculates real-time prompt/completion token spend and logs costs. |
| **Stage 3: Vector HNSW Indexing & RAG** | [`extractor/rag.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/rag.py) | • Executes semantic boundary chunking (`chunk_document_semantically`).<br>• Generates text embeddings and writes to SurrealDB HNSW vector index.<br>• Manages TTL-enforced RAG cache (`upsert_rag_cache`) for fast semantic retrieval. |

---

## 🧰 Technology Stack Inventory

| Component Layer | Technology / Tool | Version / Details | Purpose |
| ----------------- | ------------------- | ------------------- | --------- |
| **Core Framework** | Python / Django | Python 3.12/3.13, Django 6.0+ | Core MVC framework, ORM, admin backend, authentication |
| **Vector Database** | SurrealDB | v3.x (HNSW Indexing) | Multi-model document database, vector similarity search, KV cache |
| **Relational Storage** | PostgreSQL / SQLite | PostgreSQL 16+ / SQLite 3 | Enterprise relational storage for users, spend logs, audit events |
| **LLM Gateway** | Google Gemini / Vertex AI / OpenRouter | Gemini 3.1 Flash Lite / 3.5 Flash, Llama 3 (OpenRouter free fallback) | Dynamic multi-provider fallback chain for document extraction |
| **Cloud Hosting** | GCP Cloud Run | Fully Managed Serverless | Zero-scale web app and worker process containers |
| **Queue & Dispatcher** | GCP Cloud Tasks | OIDC Authenticated Tasks | Production asynchronous queue with localized thread fallbacks |
| **Object Storage** | Google Cloud Storage | GCS Bucket (`google-cloud-storage`) | Secure cloud asset storage for raw and processed documents |
| **DevSecOps & SAST** | SonarQube / Bandit / Hadolint / Desloppify | Sonar MQR Gate, Hadolint Docker | 5-phase shift-left security verification and code quality gate |

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

### 🧪 5-Phase Verification Gate (`run_checks.sh`)

Execute the local verification script to validate all quality gates prior to opening a Pull Request:

```bash
bash run_checks.sh --autofix
# OR the full pipeline (includes git pull, Desloppify, and SonarQube submission):
bash scripts/verify-pipeline.sh
```

`run_checks.sh` executes the 5 phase quality gate pipeline in sequence:

1. **Phase 1: Code Formatting & Syntax**:
   - Ruff AST Formatter (`ruff format --check .`)
   - Ruff Cyclomatic Complexity & Linter (`ruff check .`)
   - Yamllint Configuration Audit & Hadolint Docker Hardening
2. **Phase 2: Static Analysis & Type Checking**:
   - Mypy Data Flow & Static Type Checker (`mypy core/ extractor/`)
   - AST-Grep Structural Pattern Auditor (`ast-grep scan`)
3. **Phase 3: Deep Security & SAST Audit**:
   - Semgrep OSS SAST Engine (`semgrep scan --config=auto`) — strict 0 findings gate
   - Bandit ReDoS & Cryptographic Vulnerability Auditor (`bandit -c bandit.yaml`)
   - Pip-Audit Supply-Chain CVE Dependency Audit (`pip-audit -r requirements.txt`)
4. **Phase 4: Runtime Verification & Test Suite**:
   - Django System Integrity Check (`python manage.py check`)
   - Django Unit Test Suite & Coverage Export (`coverage run manage.py test` & `coverage.xml`)
5. **Phase 5: Documentation Governance & SonarQube Gate**:
   - Markdownlint Syntax Auditor (`markdownlint README.md gcp_deployment_guide.md`)
   - Automated Version & Metadata Synchronizer (`python scripts/update_docs.py`)

### 🔒 Remote 3-Phase CI/CD Pipeline

Every commit pushed to GitHub automatically triggers the remote CI/CD workflow (`.github/workflows/ci.yml`):

1. **Pre-Scan Validation**: Lints shell scripts and container files (`hadolint`).
2. **SonarQube Deep SAST**: Scans code on `https://sonarqube.fainko.cloud` using Sonar agentic AI rules with `coverage.xml`.
3. **Quality Gate Gatekeeper**: Verifies 0 Blocker/High security issues before permitting merge.

---

## 🚀 Local Development Setup Guide

### 1. Prerequisites

- Python 3.12 or 3.13 installed
- Docker & Docker Compose (optional for SurrealDB)

### 2. Environment Configuration

Create a `.env` file in the root directory:

```env
DJANGO_SECRET_KEY="your-secure-development-secret-key"
DJANGO_DEBUG=True
DJANGO_ALLOWED_HOSTS="localhost,127.0.0.1"
GEMINI_API_KEY="your-gemini-api-key"
SURREAL_URL="http://localhost:8001"
SURREAL_USER="root"
SURREAL_PASS="root"
SURREALDB_OFFLINE=False
```

### 3. Install Dependencies & Initialize Database

```bash
# Recommended: use uv (fast Python package manager)
# Install uv: https://docs.astral.sh/uv/getting-started/installation/
uv venv .venv
source .venv/bin/activate

# Install requirements
uv pip install -r requirements.txt

# Run migrations & initialize SurrealDB
python manage.py migrate
python init_surreal.py
```

> **Alternative (standard venv):** `python -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt`

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

To monitor Cloud Run revisions, inspect container metrics, and tail live logs:

```bash
bash scripts/gcp-diagnostics.sh
```

---

## 📄 License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**. See [`LICENSE`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/LICENSE) for full details.
