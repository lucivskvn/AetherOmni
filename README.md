# AetherOmni

AetherOmni is a secure, high-performance web application and background worker architecture designed for semantic document extraction, RAG querying, and memory-grounded Q&A.

<img src="scorecard.png" width="100%">

[![Version](https://img.shields.io/badge/version-v1.1.0-blue.svg)](https://github.com/aetheromni/aetheromni)
[![Last Updated](https://img.shields.io/badge/last%20updated-2026--07--10-green.svg)](#)

---

## 🚀 Key Features

* **Grounded RAG Pipeline**: Combines document chunking, text-embedding-004 vector embeddings, SurrealDB KV caching, and **Google Gemini 3.5 Flash** models for accurate, context-bound Q&A.
* **Modern LLM Stack**: Migrated to Gemini 3.x generation, using **Gemini 3.5 Flash** as the primary workhorse and **Gemini 3.1 Flash-Lite** for budget-optimized tasks.
* **Mem0 Hybrid Memory**: Maintains long-term user style and formatting preferences with an LLM token-saving write gate and strict multi-tenant context isolation.
* **Batched Database Insertion**: Optimized SurrealDB ingestion with batched payloads to prevent HTTP 413 errors on large documents.
* **Double-Tier Caching**: Uses exact-match SurrealDB KV caching and cosine-distance vector semantic caching (`RAGQueryCache`) to bypass LLM generation costs for repeated queries.
* **Dual Container Architecture**: Designed for deployment on **Google Cloud Run** as a web handler (`aetheromni-web`) and a steady task queue listener (`aetheromni-worker`).

---

## 🔒 Security & Compliance Frameworks

This project is hardened to comply with leading security standards:

* **SOC 2 Type II (CC7.1, CC7.2)**: Automated vulnerability scanning (SAST and SCA) embedded directly inside the deployment pipelines.
* **ISO 27001 (A.12.6.1, A.14.2.1)**: Formal technical vulnerability management policies and secure development standards.
* **GDPR (Data Minimization & Erasure)**: Short-lived media download links, automated text scrubbing, and dedicated data purge jobs.
* **MITRE ATLAS**: Explicitly protected against AI-specific threats:
  * *AML.T0015 (Prompt Injection)*: Dual-system prompt boundaries and sandbox validations.
  * *AML.T0004 (Data Poisoning)*: Strict file-hash checks and duplicate booklet rejection gates.
  * *AML.T0018 (Adversarial Data Leakage)*: Multi-tenant database boundary partitions.

---

## 🛡️ Pre-Check QA Gates

We use a 6-gate unified pre-check QA runner to prevent bugs or security leaks from reaching production:

```bash
./run_checks.sh
```

### Gates Description:
1. **Ruff Linter**: Enforces clean Python syntax and coding rules.
2. **Ruff Formatter**: Assures code layout and indent consistency.
3. **Django Integrity**: Verifies database schemas, migrations, and settings validations.
4. **Django Unit Tests**: Executes 102 comprehensive unit tests with coverage generation.
5. **Bandit SAST**: Scans code for security vulnerabilities.
6. **Pip-Audit SCA**: Queries the PyPI database to block any insecure or outdated dependencies.

---

## 📊 Quality & Code Health Scans

### Desloppify
Run the local code health checks:
```bash
.venv/bin/python3 -m desloppify scan --skip-slow
```
* Current Objective/Mechanical Score: **88.2%**
* Current Security Rating: **93.9%**

### SonarQube
Run the pre-production Sonar scan:
```bash
# 1. Run tests with coverage tracking
DATABASE_URL=sqlite:///db.sqlite3 .venv/bin/python3 -m coverage run --source='.' manage.py test --keepdb
.venv/bin/python3 -m coverage xml -o coverage.xml

# 2. Re-map coverage paths and trigger scanner CLI
./run_sonar_scanner.sh
```

---

## ☁️ Deployment

For detailed production deployment instructions on Google Cloud Run, PostgreSQL, and Cloud Storage, please refer to the [Google Cloud Run Deployment Guide](https://github.com/lucivskvn/AetherOmni/wiki/GCP-Cloud-Run-Deployment-Guide).

---

## 📜 License

This project is licensed under the **Universal Software Commons Trust (GNU AGPL-3.0)**. The software is endowed as a perpetual Digital Commons for the collective benefit of all humanity, preventing commercial privatization or boundary restrictions behind network services. For details, see the [LICENSE](LICENSE) file.
