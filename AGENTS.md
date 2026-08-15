# 🤖 Unified Multi-AI & AI Coding Assistant Instructions (AGENTS.md)

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Desloppify Sensor Suite, SonarQube MQR Gatekeeper.

---

## 🎯 MANDATORY WORKFLOW FOR AI AGENTS

Use `.agents/skills/aetheromni-delivery/SKILL.md` as the concise operational
runbook. This file remains the authoritative cross-agent policy.

### 1. Shift-Left Local Verification FIRST (Multi-Language Stack & Dual Git Hooks)

- **MANDATORY BEFORE CREATING ANY PULL REQUEST OR COMMITTING CODE**:
  - **Pre-Commit Gatekeeper Hook**: Local commits are enforced by `.git/hooks/pre-commit` which runs `bash run_checks.sh --fast`. This differential gate checks modified files with Ruff, ESLint, strict Sonar-aligned YAML linting, markdownlint, SurrealQL validation, Bandit, Semgrep, AST-Grep regex rules, and ShellCheck where applicable. The AST-Grep rules block recurring regex complexity and backtracking findings before a push; new suppressions require a precise rule ID, with Semgrep and SonarQube suppressions also requiring a reason.
  - **Pre-Push Gatekeeper Hook**: Local pushes are enforced by `.git/hooks/pre-push` running the full verification suite across Python (`ruff`, `mypy`, `bandit`), JavaScript (`eslint`), YAML (`yamllint`), Docker (`hadolint`), SurrealQL (`surreal validate`), and AST pattern rules (`ast-grep`).
  - **Full Suite Run**: Execute `bash run_checks.sh` locally for the complete verification pass. Shell pipeline failures must propagate so log capture cannot mask a failed check.
  - **Runtime Alignment**: Create the local environment with the interpreter declared by `pyproject.toml` and install `requirements-dev.txt`; `run_checks.sh` rejects an incompatible interpreter rather than silently producing incompatible results.
  - **Active Linters & Auto-Fixers**: Ensure active auto-fixers (`markdownlint --fix`, `yamllint`, `ruff check --fix`, `ruff format`) are executed so document formatting, JS/Python code standards, and YAML schemas are automatically corrected.
- You MUST ensure the verification suite passes cleanly with **0 Blocker / High Security Vulnerabilities** and **0 Complexity Errors** BEFORE creating or opening a Pull Request.

### 2. Mandatory Automatic Documentation & Steering Synchronization

- **STRICT NON-STALE CONTINUITY MANDATE**:
  - Whenever linters, cognitive complexity rules, Python/JS versions, workflow steps, or architecture patterns are added or modified, you MUST **automatically update all steering and project documents** (`AGENTS.md`, `.cursorrules`, `.github/copilot-instructions.md`, `README.md`, and `docs/gcp_deployment_guide.md`) in the **exact same session/turn**.
  - **Zero Stale Information Policy**: Never hardcode specific numbers — test counts, rule counts, score values, or tool version strings — in documentation. Use dynamic badges and auto-generated images (`scorecard.png`) instead.

### 3. Desloppify Codebase Health & Sensor Audit

- Run `desloppify scan` to audit structural complexity, responsibility cohesion, dependency cycles, duplicate logic, and code health metrics.
- Use the generated `docs/scorecard.png` as the source of truth for the health target and status. Use the runtime declared by `pyproject.toml` and the digest-pinned `Dockerfile` image.
- Never hardcode Desloppify scores in Markdown — always reference the dynamic `docs/scorecard.png` badge image.

### 4. Automated Cloud SAST & Quality Gate (SonarQube & Semgrep)

- Once `run_checks.sh` passes locally and the user approves remote pushing, push to `origin main` or open a PR.
- The 3-phase GitHub Actions pipeline will automatically trigger:
     1. Pre-Scan Validation — blocking Hadolint + shell script syntax check
     2. Community Edition PR Shift-Left Gate — runs Ruff, ESLint, strict Sonar-aligned YAML linting, AST-Grep regex rules, Semgrep, Bandit, tests, CodeQL, and external security checks on pull requests; SonarQube Cloud SAST analyzes `main` after a push with multi-language Python and JavaScript coverage.
     3. Post-Scan Quality Gate Gatekeeper — publishes the actionable condition table in both the Actions log and summary, annotates failures, and blocks violations
  - Cloud Build steps that source computed metadata must use Kaniko's BusyBox-enabled debug image pinned by immutable digest; the standard executor image has no shell. Use a registry-backed Kaniko cache and bounded image, filesystem, and push retries.
  - Cloud Build may construct the immutable image in parallel, but it must wait for a successful GitHub mainline SonarQube check on the exact commit SHA before either Cloud Run deployment. Manual builds must provide a previously verified commit SHA.
  - Keep CI security scanners in an isolated virtual environment when their dependency graph differs from the application runtime; the scan must remain blocking.
  - Pin every GitHub Action to a full commit SHA, retaining the reviewed release tag only as an adjacent comment.

### 4.1 Protected PR Branch Refresh

- `.github/workflows/refresh-pr-branches.yml` refreshes only open, non-draft,
  same-repository PRs targeting `main` after a mainline push or manual dispatch.
- It never merges PRs or relaxes required checks. Configure
  `PR_AUTOMATION_TOKEN` as a dedicated fine-grained GitHub App/PAT token with
  only `contents: write` and `pull-requests: write` repository access so its
  branch updates trigger fresh PR checks.

### 5. Local-First Review & No Unsolicited Remote Pushing

- **DO NOT AUTOMATICALLY PUSH INCREMENTAL EDITS TO REMOTE GITHUB**:
     Keep all commits and code modifications local for explicit user review (`git status`, `git diff`). Never execute `git push` unless specifically requested by the user.

### 6. Fork & Upstream PR Safety Guard

- **DO NOT TRIGGER OR TARGET UPSTREAM ORIGIN PARENT REPOSITORIES ON FORKS**:
     All PRs, branches, and commits MUST target `origin` directly.

### 7. Release, GCP, and Agent Hand-off

- Compute the release version before SonarQube analysis and Cloud Build; never
  manually edit a release version or deploy a `latest` fallback.
- Cloud Build trigger checkouts are shallow. Unshallow them before deriving the
  commit-count patch so SonarQube, Cloud Run, image tags, and the UI use one version.
- Supabase CAPTCHA tokens must be sent in GoTrue `gotrue_meta_security`; require
  Turnstile before credential dispatch and grant admin only through `ADMIN_EMAIL`
  or server-controlled Supabase app metadata. Never auto-promote the first user.
- In production, use the Supabase Auth subject UUID for document ownership,
  tenant filtering, exports, RAG access, and rate-limit keys. Django/SQLite IDs
  are local implementation details and are permitted only in explicit offline mode.
- Default the worker to Cloud Run on-demand scaling (minimum zero, bounded
  maximum) and disable periodic maintenance. Cloud Tasks wakes the worker for
  queued work; opt into an always-on worker only when scheduled maintenance is
  explicitly required. Web instances must not start maintenance threads.
- Cloud Tasks must dispatch only to `WORKER_URL` in production; never fall back
  to a local web-process thread. Cloud Build resolves the worker URL and GCP
  project identity at deploy time, while `_APP_URL` may set the public Supabase
  confirmation origin.
- Treat the GitHub Actions summary as the actionable SonarQube hand-off. Keep
  failures blocking so Jules can address scoped issues from PR checks or issues.
- SonarQube Community Edition does not support pull-request or branch analysis.
  Never pass pull-request identity parameters or analyze a merge checkout as the
  default branch; use the GitHub PR shift-left gate and reserve SonarQube for main.
  PR Actions must publish the read-only `main` quality-gate baseline as a table,
  clearly labeled as baseline context rather than a PR result.
- Use `scripts/gcp-diagnostics.sh` only for read-only Cloud Run diagnosis. Do
  not reintroduce secret-retrieval or imperative provisioning scripts.
- Use Pulumi for new or rebuilt GCP environments. Import and preview existing
  infrastructure before applying changes.

---

## 🛡️ Primary DevSecOps & Architectural Standards

1. **Formal Standardized Batch Export Naming**:
   - All batch exports must follow the formal sequential 3-digit index pattern: `<3-DIGIT_INDEX>_<CLEAN_TITLE_SLUG>.md` (e.g., `001_enterprise_legal_contract.md`).

2. **Dynamic GCP & Secret Manager Resolution**:
   - Never hardcode GCP project IDs, project numbers, or API keys (`OPENROUTER_API_KEY`) in committed manifests (`service.yaml`, `service-worker.yaml`, scripts, or database models). Sourced dynamically from environment variables or GCP Secret Manager.

3. **Dynamic Documentation & Zero Static Numbers**:
   - Avoid hardcoding static version strings, score numbers, test counts, or rule counts in Markdown text or tables. Use dynamic badges or auto-generated images (`scorecard.png`) to prevent stale documentation.

4. **Remote Self-Hosted SonarQube Architecture (`sonarqube.fainko.cloud`)**:
   - Target Host: `https://sonarqube.fainko.cloud` (Coolify Cloudflare Tunnel).
   - Zero local server footprint: Reclaims local RAM and CPU cycles.

5. **SurrealDB Native Transactions, Schema Validation & Async Safety**:
   - Execute budget caps and atomic counters using SurrealDB native `BEGIN TRANSACTION ... COMMIT TRANSACTION;` blocks.
   - Validate all `.surql` files with `surreal validate` before committing. Integrated into Phase 2 of `run_checks.sh` and the fast `--fast` differential pass.
   - Use `concurrent.futures.ThreadPoolExecutor` worker dispatch in sync/async boundaries instead of monkeypatching event loops with `nest_asyncio`.

6. **Multi-Language Code Quality & Eco-Design**:
   - Python: Target Python 3.14 across Docker, CI, local checks, and SonarQube, with `ruff`, `mypy`, and `bandit`.
   - JavaScript: ESLint 10 with SonarQube JS conventions (`?.` optional chaining, global scope checks, complexity ≤ 15).
   - SurrealQL: `surreal validate` enforces `.surql` schema syntax on every pipeline run.
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings.

7. **Dual-Tier State Architecture & Ephemeral Cloud Run Persistence**:
   - Relational Identity & Auth: Supabase PostgreSQL (`SUPABASE_DATABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`) securely manages users, sessions, and administrative privileges.
   - High-Throughput & Vector Engine: Remote SurrealDB (`wss://surrealdb.fainko.cloud/rpc`) manages documents, vector chunks (HNSW 768 cosine), tokenized prompt caches (`context_cache`), distributed atomic sliding-window quotas (`rate_limits`), user memories (`user_memories`), and compliance audit logs.
   - Zero Ephemeral State Loss: Cloud Run container instances remain strictly stateless, eliminating data loss on container restarts. Local SQLite is restricted to offline testing (`SURREALDB_OFFLINE=True`).

---

## 📋 Standardized AI Agent Prompt & Steering Template

When issuing tasks to AI coding assistants (Antigravity, Jules, Codex, Claude, Kiro), use the following standardized prompt template:

```text
As an AI coding assistant, execute the requested task following these strict repository rules:
1. Shift-Left Local Verification FIRST: Execute `bash run_checks.sh` for source code changes
   (runs Python ruff/mypy/bandit, JS eslint, YAML yamllint, Hadolint, surreal validate),
   or `bash run_checks.sh --fast` for documentation/chore edits.
2. Automatic Documentation Sync: Automatically update all steering docs (AGENTS.md,
   .cursorrules, copilot-instructions.md, README.md, docs/gcp_deployment_guide.md) whenever
   linter configurations, runtime rules, or architectural decisions change.
3. Local-First Review: Keep all commits and code modifications strictly local on the working
   branch. NEVER execute `git push` unless explicitly commanded by the user.
4. Desloppify Health & Security: Use `docs/scorecard.png` for current health and security status
   and the canonical project configuration for the runtime. Never hardcode scores in documentation.
5. Active Linters & Auto-Fixers: Ensure `markdownlint --fix`, `yamllint`, `npx eslint`,
   `ruff check --fix`, `ruff format`, and `surreal validate` are actively executed across all
   files without exception.
6. Dynamic Documentation: Avoid static version/score numbers in Markdown text or tables;
   use dynamic scorecard images (scorecard.png) and badges.
   Release versions must be computed as MAJOR.MINOR.PATCH before SonarQube analysis and deployment.
7. Standardized Model & Auth Naming: Use Gemini 2.5 Flash / 2.5 Flash-Lite for the stable Vertex v1 production path; preview-model adoption requires an availability check. Refer to Vertex AI Vision, GCP Secret Manager, and Supabase Auth Platform (Supabase Cloud).
```

<!-- sonar:begin:codex-secrets-on-read -->
## 🔒 SonarQube Secrets Scanning for Files Protocol

Before reading any file in this workspace, scan it for secrets with the deterministic scanner:

```bash
sonar analyze secrets <path/to/file>
```

If the command reports that the file contains a secret, **do not read the file**. Instead:

1. Inform the user that the file appears to contain a secret or credential and that reading it would expose the value in chat history, logs, and any downstream telemetry.
2. Advise them to rotate the leaked credential at its source of truth and remove it from the file.
3. Do not proceed with the original request until the secret has been removed.
<!-- sonar:end:codex-secrets-on-read -->
