# 🤖 Unified Multi-AI & AI Coding Assistant Instructions (.github/copilot-instructions.md)

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Desloppify Sensor Suite, SonarQube MQR Gatekeeper.

---

## 🎯 MANDATORY WORKFLOW FOR AI AGENTS (Antigravity & Jules AI)

### 1. Shift-Left Local Verification FIRST

- **MANDATORY BEFORE CREATING ANY PULL REQUEST OR COMMITTING CODE**:
  - **Source Code Changes**: Execute `bash run_checks.sh` locally first to run the full 5-phase verification suite.
  - **Documentation & Chore Edits Only**: Execute `bash run_checks.sh --fast` (or `--docs`) to verify markdown syntax & sync release metadata in <1s without wasting time running heavy unit tests.
  - **Active Linter & Auto-Fixers**: Ensure active auto-fixers (`markdownlint --fix`, `yamllint`, `ruff check --fix`, `ruff format`) are executed so document formatting and YAML schemas are automatically corrected.
- You MUST ensure the verification suite passes cleanly with **0 Blocker / High Security Vulnerabilities** BEFORE creating or opening a Pull Request.

### 2. Desloppify Codebase Health & Sensor Audit

- Run `desloppify scan` to audit structural complexity, responsibility cohesion, dependency cycles, duplicate logic, and code health metrics across all 17 sensors.
- Maintain objective mechanical health score >= 85.0. Target runtime: Python 3.13 (`py313`).

### 3. Automated Cloud SAST & Quality Gate (SonarQube & Semgrep)

- Once `run_checks.sh` passes locally and the user approves remote pushing, push to `origin main` or open a PR.
- The 3-phase GitHub Actions pipeline will automatically trigger:
     1. Pre-Scan Validation
     2. 1. SonarQube Cloud SAST (Self-Hosted) on `https://sonarqube.fainko.cloud` (Sonar agentic AI rules with Python 3.13 `coverage.xml`)
     3. Post-Scan Quality Gate Gatekeeper

### 4. Local-First Review & No Unsolicited Remote Pushing

- **DO NOT AUTOMATICALLY PUSH INCREMENTAL EDITS TO REMOTE GITHUB**:
     Keep all commits and code modifications local for explicit user review (`git status`, `git diff`). Never execute `git push` unless specifically requested by the user.

### 5. Fork & Upstream PR Safety Guard

- **DO NOT TRIGGER OR TARGET UPSTREAM ORIGIN PARENT REPOSITORIES ON FORKS**:
     All PRs, branches, and commits MUST target `origin` directly.

---

## 🛡️ Primary DevSecOps & Architectural Standards

1. **Formal Standardized Batch Export Naming**:
   - All batch exports must follow the formal sequential 3-digit index pattern: `<3-DIGIT_INDEX>_<CLEAN_TITLE_SLUG>.md` (e.g., `001_enterprise_legal_contract.md`).

2. **Dynamic GCP & Secret Manager Resolution**:
   - Never hardcode GCP project IDs, project numbers, or API keys (`OPENROUTER_API_KEY`) in committed manifests (`service.yaml`, `service-worker.yaml`, scripts, or database models). Sourced dynamically from environment variables or GCP Secret Manager.

3. **Dynamic Documentation & Zero Static Numbers**:
   - Avoid hardcoding static version strings, score numbers, or test counts in Markdown tables. Use dynamic badges or auto-generated images (`scorecard.png`) to prevent stale documentation.

4. **Remote Self-Hosted SonarQube Architecture (`sonarqube.fainko.cloud`)**:
   - Target Host: `https://sonarqube.fainko.cloud` (Coolify Cloudflare Tunnel, Python 3.13 runtime).
   - Zero local server footprint: Reclaims local RAM and CPU cycles.

5. **SurrealDB Native Transactions & Async Safety**:
   - Execute budget caps and atomic counters using SurrealDB native `BEGIN TRANSACTION ... COMMIT TRANSACTION;` blocks.
   - Use `concurrent.futures.ThreadPoolExecutor` worker dispatch in sync/async boundaries instead of monkeypatching event loops with `nest_asyncio`.

6. **Code Quality & Eco-Design**:
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings. Target runtime: Python 3.13 (`py313`).

---

## 📋 Standardized AI Agent Prompt & Steering Template

When issuing tasks to AI coding assistants (Antigravity, Jules, Codex, Claude, Kiro), use the following standardized prompt template:

```text
As an AI coding assistant, execute the requested task following these strict repository rules:
1. Shift-Left Local Verification FIRST: Execute `bash run_checks.sh` locally for source code changes, or `bash run_checks.sh --fast` for documentation/chore edits (<1s execution).
2. Local-First Review: Keep all commits and code modifications strictly local on the working branch. NEVER execute `git push` unless explicitly commanded by the user.
3. Desloppify Health & Security: Maintain objective mechanical health score >= 85.0 and security score = 100%. Target runtime: Python 3.13 (py313).
4. Active Linters & Auto-Fixers: Ensure `markdownlint --fix`, `yamllint`, `ruff check --fix`, and `ruff format` are actively executed across all files without exception.
5. Dynamic Documentation: Avoid static version/score numbers in Markdown text or tables; use dynamic scorecard images (scorecard.png) and badges.
6. Standardized Model & Auth Naming: Always reference Gemini 3.6 Flash / 3.5 Flash-Lite, Vertex AI Vision, GCP Secret Manager, and Supabase Auth Platform (Supabase Cloud).
```
