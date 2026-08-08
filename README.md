# 🚀 AetherOmni — Enterprise Multi-Model RAG & Document Intelligence Platform

> **Production-grade Django 6.x platform featuring Multi-Model LLM Gateways, Dual Database Engine (SurrealDB HNSW Vector RAG + Relational Store), Async 3-Stage Processing Pipelines, and Serverless Cloud Native Infrastructure.**

[![Build Status](https://img.shields.io/badge/CI%2FCD-Passing-brightgreen.svg)](https://github.com/lucivskvn/AetherOmni/actions)
[![Tests](https://img.shields.io/badge/Tests-181%20Passed-success.svg)](#-devsecops--quality-gates)
[![Python Version](https://img.shields.io/badge/Python-3.12%20%7C%203.13-blue.svg)](https://www.python.org/)
[![Django Version](https://img.shields.io/badge/Django-6.0%2B-092E20.svg)](https://www.djangoproject.com/)
[![Database Engine](https://img.shields.io/badge/Vector%20DB-SurrealDB%20v3.x%20HNSW-ff0055.svg)](https://surrealdb.com/)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

---

## 📌 Executive Summary & Core Program Goals

**AetherOmni** is an enterprise-grade document extraction, layout parsing, and Retrieval-Augmented Generation (RAG) platform designed for high-concurrency document processing across heterogeneous formats (PDF, DOCX, CSV, TXT, and ZIP archives).

### 🎯 Primary Program Goals & Target Use Cases

1. **Enterprise Document Layout Extraction**:
   - **Use Case**: Legal, historical, and corporate document archiving.
   - **Goal**: Preserve multi-column structures, embedded tables, and Right-to-Left (RTL) Arabic typography (`dir="rtl" class="arabic-text"`) with zero structural loss.

2. **Cost-Controlled Multi-Model LLM Routing**:
   - **Use Case**: Multi-tenant SaaS & high-volume prompt execution.
   - **Goal**: Automatically dispatch prompt execution across Google Gemini 3.6 Flash / 3.5 Flash-Lite, Google Cloud Vertex AI, and OpenRouter (Llama 3 8B, Gemma 2 9B, Qwen 2 7B free tier fallbacks) while enforcing hard monthly USD spend limits (`MonthlySpendLog`).

3. **Ultra-Low-Latency Hybrid Vector RAG**:
   - **Use Case**: Enterprise knowledge bases & automated QA dataset creation.
   - **Goal**: Deliver sub-100ms vector similarity searches using SurrealDB HNSW indexing combined with TTL-enforced semantic cache layers (`upsert_rag_cache`).

4. **Zero-Trust Cloud Serverless Deployment**:
   - **Use Case**: Production deployments requiring high security and autoscaling.
   - **Goal**: Deploy on GCP Cloud Run with OIDC-authenticated Cloud Tasks queues, non-root container isolation (`django-user`), and Knative auto-scaling (`minScale`, `maxScale`).

---

## ⚡ Current Functional Capabilities (Current State v1.2.333)

| Feature Area | Current Production Capability | Implementation & Location |
|--------------|--------------------------------|---------------------------|
| **Multi-Format Ingestion** | Ingests PDF, DOCX, CSV, TXT, and recursive ZIP batch archives. | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) |
| **Arabic & Multilingual RTL** | Automatic Arabic typography detection (`dir="rtl" class="arabic-text"`), Markdown rendering, HTML sanitization. | `parse_arabic_layout` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L48) |
| **Multi-Model LLM Gateway** | Dynamic provider fallbacks across Gemini 3.6 Flash / 3.5 Flash-Lite, Vertex AI (multi-region), and OpenRouter (Llama 3, Gemma 2, Qwen 2 free tiers). | `generate_llm_content_unified` in [`llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) |
| **SurrealDB HNSW Vector RAG** | High-dimensional HNSW similarity search, document UUID scope filtering, and TTL semantic cache. | `search_rag_cache_hnsw` in [`surreal_db.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/surreal_db.py#L880) |
| **Persisted Budget Accounting** | Hard monthly USD budget caps; document deletion spend is persisted to `MonthlySpendLog`. | `MonthlySpendLog.add_cost()` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L865) |
| **Curated ZIP Bundling** | Filtered document subset exports organized into `Language/` and `Author/` taxonomies with `manifest.json`. | `generate_curated_zip_bundle` in [`file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py#L322) |
| **SOC 2 Immutable Audit Trail** | Logs user IDs, client IPs (`get_client_ip`), actions, and timestamps in an immutable ledger. | `AuditLogListView` in [`views.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/views.py#L1520) |
| **7-Layer DevSecOps Verification** | 181 passing unit tests, Hadolint, Bandit, AST-Grep, SonarQube MQR Gate, Desloppify (mechanical score: 88.1/100; overall strict requires subjective review pass). | `scripts/verify-pipeline.sh` & `.github/workflows/ci.yml` |

---

## 🗺️ Next Milestones & Roadmap (On Progress / Future)

```mermaid
flowchart LR
    subgraph M2 ["Milestone 2.0 (Completed)"]
        M2A["Dual DB Engine<br>SurrealDB HNSW + Relational"]
        M2B["Multi-Model Gateway<br>Gemini + Vertex AI + OpenRouter"]
        M2C["7-Layer DevSecOps<br>181 Tests + SonarQube Gate"]
    end

    subgraph M3 ["Milestone 3.0 (Current Sprint)"]
        M3A["SurrealDB Native SDK<br>WebSocket Pool Migration"]
        M3B["Hybrid RAG Search<br>BM25 + HNSW Fusion"]
        M3C["Multi-Modal OCR<br>Gemini / Vertex Vision"]
    end

    subgraph M4 ["Milestone 4.0 (Planned Phase)"]
        M4A["Real-time SSE Streaming<br>Chunked Response Delivery"]
        M4B["Enterprise RBAC<br>Tenant Scoped Permissions"]
        M4C["Automated RAG Eval<br>RAGAS & TruLens Pipeline"]
    end

    M2 --> M3 --> M4
```

### 🔄 Milestone 3.0 (In Progress — Active Sprint)
- [x] **Native SurrealDB Python SDK Integration**: Upgrading SurrealDB REST HTTP client to native WebSocket connection pools (`surrealdb-python`).
- [ ] **Hybrid Dense-Sparse RAG Search (BM25 + HNSW)**: Implementing Reciprocal Rank Fusion (RRF) to merge exact keyword BM25 matches with dense vector embeddings.
- [ ] **Multi-Modal Diagram & Schema OCR**: Extracting embedded flowcharts, tables, and architectural diagrams using Gemini Vision / Vertex AI Vision.

### 🎯 Milestone 4.0 (Planned — Future Phase)
- [ ] **Real-time Streaming RAG Responses**: Server-Sent Events (SSE) / WebSocket streaming for real-time response rendering in the dashboard.
- [ ] **Enterprise RBAC & Document ACLs**: Fine-grained role-based access control with organizational tenant scoping.
- [ ] **Automated RAG Benchmarking Pipeline**: Integration of RAGAS and TruLens for continuous assessment of context precision, answer relevance, and faithfulness.

---

## 🏗️ System Architecture & Data Flow

```mermaid
flowchart TD
    subgraph ClientLayer ["Client & Ingestion Layer"]
        User["Web UI / REST API"]
        Upload["Document Upload (PDF, DOCX, CSV, TXT, ZIP)"]
        User --> Upload
    end

    subgraph WebLayer ["Django Web App (GCP Cloud Run Web)"]
        DjangoView["Django View Controller (views.py)"]
        BudgetGate["Budget & Rate Gatekeeper (llm_gateway.py)"]
        Upload --> DjangoView
        DjangoView --> BudgetGate
    end

    subgraph QueueLayer ["Async Dispatcher"]
        CloudTasks["Google Cloud Tasks (OIDC Auth) / Local Thread"]
        BudgetGate -->|Dispatch Ingestion| CloudTasks
    end

    subgraph WorkerLayer ["Async Processing Pipeline (tasks.py)"]
        Stage1["Stage 1: Layout & RTL Parser (file_utils.py)"]
        Stage2["Stage 2: LLM Refinement Gateway (llm_gateway.py)"]
        Stage3["Stage 3: Semantic Chunking & Vector HNSW (rag.py)"]
        
        CloudTasks --> Stage1
        Stage1 --> Stage2
        Stage2 --> Stage3
    end

    subgraph DataLayer ["Persistence & Vector Store"]
        SurrealDB[("SurrealDB v3.x — HNSW Vector Index & Cache")]
        SQLDB[("Relational Store - PostgreSQL / SQLite")]
        GCS[("Cloud Storage - GCP Bucket")]
        
        Stage3 --> SurrealDB
        Stage3 --> SQLDB
        Stage1 --> GCS
    end

    subgraph LLMProviders ["Multi-Model LLM Gateway Providers"]
        Gemini["Google Gemini 3.1 Flash Lite / 3.5 Flash (AI Studio)"]
        Vertex["Google Cloud Vertex AI (multi-region)"]
        OpenRouter["OpenRouter (Llama 3 free tier fallback)"]

        Stage2 <--> Gemini
        Stage2 <--> Vertex
        Stage2 <--> OpenRouter
    end
```

---

## 🔄 3-Stage Async Ingestion Pipeline Breakdown

| Pipeline Stage | Module Location | Primary Technical Responsibilities |
|----------------|-----------------|-----------------------------------|
| **Stage 1: Layout & Ingestion** | [`extractor/file_utils.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/file_utils.py) | • Validates document headers, sanitizes HTML, computes SHA-256 hashes.<br>• Parses Arabic RTL typography (`parse_arabic_layout`) and extracts YAML frontmatter.<br>• Unpacks ZIP archives recursively into structured folder taxonomies (`Language/`, `Author/`). |
| **Stage 2: LLM Refinement & Cost Control** | [`extractor/llm_gateway.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/llm_gateway.py) | • Evaluates `check_budget_and_api_limit()` against `MonthlySpendLog` USD caps.<br>• Dispatches prompts across primary LLM providers with exponential backoff.<br>• Calculates real-time prompt/completion token spend and logs costs. |
| **Stage 3: Vector HNSW Indexing & RAG** | [`extractor/rag.py`](file:///media/elang/TMSSD/CrossSharing/Repos/AetherOmni/extractor/rag.py) | • Executes semantic boundary chunking (`chunk_document_semantically`).<br>• Generates text embeddings and writes to SurrealDB HNSW vector index.<br>• Manages TTL-enforced RAG cache (`upsert_rag_cache`) for fast semantic retrieval. |

---

## 🧰 Technology Stack Inventory

| Component Layer | Technology / Tool | Version / Details | Purpose |
|-----------------|-------------------|-------------------|---------|
| **Core Framework** | Python / Django | Python 3.12/3.13, Django 6.0+ | Core MVC framework, ORM, admin backend, authentication |
| **Vector Database** | SurrealDB | v3.x (HNSW Indexing) | Multi-model document database, vector similarity search, KV cache |
| **Relational Storage** | PostgreSQL / SQLite | PostgreSQL 16+ / SQLite 3 | Enterprise relational storage for users, spend logs, audit events |
| **LLM Gateway** | Google Gemini / Vertex AI / OpenRouter | Gemini 3.1 Flash Lite / 3.5 Flash, Llama 3 (OpenRouter free fallback) | Dynamic multi-provider fallback chain for document extraction |
| **Cloud Hosting** | GCP Cloud Run | Fully Managed Serverless | Zero-scale web app and worker process containers |
| **Queue & Dispatcher** | GCP Cloud Tasks | OIDC Authenticated Tasks | Production asynchronous queue with localized thread fallbacks |
| **Object Storage** | Google Cloud Storage | GCS Bucket (`google-cloud-storage`) | Secure cloud asset storage for raw and processed documents |
| **DevSecOps & SAST** | SonarQube / Bandit / Hadolint / Desloppify | Sonar MQR Gate, Hadolint Docker | 7-layer shift-left security verification and code quality gate |

---

## ✨ Core Feature Matrix

- 🌐 **Multilingual & RTL Layout Preservation**: Full support for Right-to-Left Arabic text formatting and multi-column document parsing.
- 📦 **Curated ZIP Archival Export**: Bundles filtered documents into organized folder hierarchies (`Language/English`, `Author/Shakespeare`) complete with `manifest.json` and combined `master_archival_source.md`.
- 🔎 **Hybrid Semantic RAG Search**: Combines SurrealDB HNSW vector search with document UUID scope filtering and score thresholding.
- 💰 **Persisted Monthly Spend Accounting**: Persists deleted document costs via `MonthlySpendLog.add_cost()` to maintain financial audit integrity even after purging records.
- 🛡️ **SOC 2 & ISO 27001 Audit Logs**: Logs client IP addresses (`get_client_ip`), user IDs, action names, and timestamps in an immutable audit trail (`AuditLogListView`).

---

## 🛡️ DevSecOps & Quality Gates

AetherOmni strictly enforces **Shift-Left Local Verification** before code can be merged into production branches.

### 🧪 Local 7-Layer Verification Gate

Execute the local verification script to validate all quality gates prior to opening a Pull Request:

```bash
bash run_checks.sh
# OR the full pipeline (includes git pull, Desloppify, and SonarQube submission):
bash scripts/verify-pipeline.sh
```

`run_checks.sh` executes these 7 steps in sequence:
1. **Ruff Linting**: `ruff check .`
2. **Ruff Format Check**: `ruff format --check .`
3. **Django System Integrity**: `python manage.py check`
4. **Django Test Suite**: `python manage.py test --keepdb` (caches test count to `.test_count`)
5. **Bandit SAST**: `python -m bandit -c bandit.yaml -r extractor/` — zero high/blocker issues required
6. **pip-audit Dependency Scan**: checks for known CVEs in installed packages
7. **Auto Documentation Update**: `python scripts/update_docs.py` — syncs version badges and service YAMLs

`scripts/verify-pipeline.sh` extends the above with SonarQube remote submission (Layer 6) and GitHub CLI PR status (Layer 7).

```
Ran 181 tests in 39.913s

OK
```

### 🔒 Remote 3-Phase CI/CD Pipeline

Every commit pushed to GitHub automatically triggers the remote CI/CD workflow:
1. **Pre-Scan Validation**: Lints shell scripts and container files.
2. **SonarQube Deep SAST**: Scans code on `https://sonarqube.fainko.cloud` using Sonar agentic AI rules.
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

