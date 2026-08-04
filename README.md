# 🚀 AetherOmni

> Django Enterprise WebApp with Multi-Model RAG & SurrealDB

[![Build Status](https://img.shields.io/badge/CI%2FCD-3--Phase%20Pipeline-blue.svg)](https://github.com/lucivskvn/AetherOmni/actions)
[![SonarQube](https://img.shields.io/badge/SonarQube-Sonar%20agentic%20AI-brightgreen.svg)](https://sonarqube.fainko.cloud)
[![Security](https://img.shields.io/badge/DevSecOps-SOC2%2FOWASP-success.svg)](#-devsecops--quality-gates)

---

## 🛠️ Local Verification & Pre-Commit Quality Gate

Before submitting a Pull Request or committing changes, run the local 7-layer verification gate:

```bash
bash scripts/verify-pipeline.sh
```

This executes:
1. Shell & Container Linter (`hadolint`, `bash -n`)
2. Static Application Security Testing (`Bandit` / `ast-grep`)
3. Code Quality & Format Audit
4. Automated Test Suite & Coverage Checks

### ☁️ GCP Live Diagnostics (Cloud Run)

To tail live logs, verify revision statuses, and diagnose Cloud Run service issues:

```bash
bash scripts/gcp-diagnostics.sh
```

---

## 🔒 3-Phase CI/CD Pipeline (GitHub Actions & SonarQube)

Every commit pushed to `main` (or `next`) automatically triggers the 3-phase scanning workflow:
1. **Pre-Scan Validation**: Fast container & script linting (`hadolint`).
2. **SonarQube Cloud SAST**: Deep static code analysis on `https://sonarqube.fainko.cloud/` enforcing `Sonar agentic AI` rules.
3. **Post-Scan Quality Gatekeeper**: Enforces remote Quality Gate compliance before PR merge.
