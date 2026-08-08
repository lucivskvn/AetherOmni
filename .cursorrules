# 🤖 Unified Multi-AI & AI Coding Assistant Instructions

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Desloppify Sensor Suite, SonarQube MQR Gatekeeper.

---

## 🎯 MANDATORY WORKFLOW FOR AI AGENTS (Antigravity & Jules AI)

### 1. Shift-Left Local Verification FIRST:
   - **MANDATORY BEFORE CREATING ANY PULL REQUEST OR COMMITTING CODE**:
     You MUST execute `bash run_checks.sh` (or `bash scripts/verify-pipeline.sh`) locally first after making any code changes.
   - You MUST ensure the 5-phase verification suite passes cleanly with **0 Blocker / High Security Vulnerabilities** (Hadolint, Bandit, AST-Grep, Mypy, Semgrep, Pytest/Coverage, Markdownlint) BEFORE creating or opening a Pull Request.

### 2. Desloppify Codebase Health & Sensor Audit:
   - Run `desloppify scan` to audit structural complexity, responsibility cohesion, dependency cycles, duplicate logic, and code health metrics across all 17 sensors.
   - Maintain objective mechanical health score >= 85.0.

### 3. Automated Cloud SAST & Quality Gate (SonarQube & Semgrep):
   - Once `run_checks.sh` passes locally, push to `origin main` or open a PR.
   - The 3-phase GitHub Actions pipeline will automatically trigger:
     1. Pre-Scan Validation
     2. Deep SonarQube SAST on `https://sonarqube.fainko.cloud` (Sonar agentic AI rules with `coverage.xml`)
     3. Post-Scan Quality Gate Gatekeeper

### 4. Fork & Upstream PR Safety Guard:
   - **DO NOT TRIGGER OR TARGET UPSTREAM ORIGIN PARENT REPOSITORIES ON FORKS**:
     All PRs, branches, and commits MUST target `origin` directly (e.g., `git push origin <branch>`).

---

## 🛡️ Primary DevSecOps & Architectural Standards

1. **Formal Standardized Batch Export Naming**:
   - All batch exports must follow the formal sequential 3-digit index pattern: `<3-DIGIT_INDEX>_<CLEAN_TITLE_SLUG>.md` (e.g., `001_enterprise_legal_contract.md`).

2. **Dynamic GCP & Secret Manager Resolution**:
   - Never hardcode GCP project IDs, project numbers, or bucket names in committed manifests (`service.yaml`, `service-worker.yaml`, scripts). Sourced dynamically from `.env` or `gcloud config` CLI.

3. **Remote Self-Hosted SonarQube Architecture (`sonarqube.fainko.cloud`)**:
   - Target Host: `https://sonarqube.fainko.cloud` (Coolify Cloudflare Tunnel).
   - Zero local server footprint: Reclaims local RAM and CPU cycles.

4. **Code Quality & Eco-Design**:
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings.
