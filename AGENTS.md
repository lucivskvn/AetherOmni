# 🤖 Unified Multi-AI & AI Coding Assistant Instructions (AGENTS.md)

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Desloppify Sensor Suite, SonarQube MQR Gatekeeper.

---

## 🎯 MANDATORY WORKFLOW FOR AI AGENTS

Use `.agents/skills/aetheromni-delivery/SKILL.md` as the concise operational
runbook. This file remains the authoritative cross-agent policy.

`run_checks.sh` is the canonical local verification entrypoint. The legacy
`scripts/verify-pipeline.sh` is a compatibility wrapper only and must not fetch,
pull, stash, perform remote scans, or deploy.

### Runtime reliability contracts

- Dashboard refresh uses complete user-scoped status snapshots and periodic polling alongside Realtime hints. Missing rows imply deletion only when the response explicitly declares completeness.
- Persist Cloud Tasks identities before dispatch. Cancellation records a durable stop marker and deletes queued work; running workers must fence writes by task identity and cancellation state. Already-running provider calls may finish before the next cancellation check.
- Acknowledge task delivery only after a durable outcome. Unhandled failures remain retryable under the Pulumi queue retry policy; failed error persistence must propagate.
- Keep document records when physical-file deletion fails, report the failure, and allow retry. Apply the additive document schema and Django migration before enabling task identity enforcement.
- Deployment scaling mutations require a superuser and target only `WORKER_SERVICE_NAME`; never substitute `WEB_SERVICE_NAME` after lookup failure. Grant task deletion on the application queue through Pulumi before rollout.
- Keep the Cloud Run worker private: deploy it with `--no-allow-unauthenticated` and invoke it only through Cloud Tasks OIDC. Production deployment must require `SUPABASE_DATABASE_URL`; SQLite is offline-only.
- The runtime service account needs `roles/run.viewer` only to read the configured worker for the superuser deployment controller; retain least privilege and do not grant editor/admin roles.
- XLSX ingestion must enforce parser-specific archive, worksheet, row, cell, and rendered-output budgets; generic upload and ZIP limits alone are insufficient.
- Reliability gates must deny production work when a distributed rate-limit backend is unavailable or returns an indeterminate result. Private document deduplication is tenant-only; shared reuse requires an explicitly modeled public corpus. The web console must never render credential inputs for deployment-managed secrets.
- In production, SurrealDB is the sole deletion-spend accounting authority; the Django `pre_delete` ledger signal is restricted to explicit offline mode so a mirrored row cannot double-count a document.
- Dashboard RAG uses the authenticated SSE endpoint when readable streams are available, renders streamed tokens as text, and falls back to the sanitized JSON response when streaming is unavailable.
- Use `scripts/normalize_system_settings.py` as a dry-run preflight for persisted settings drift; `--apply` requires reviewed deployment approval, must never print setting values, must verify its postconditions, and must purge retired `openrouter_api_key` data before the corresponding schema release.
- Preserve the Cloud Run memory floor in Pulumi, declarative service manifests, and Cloud Build deploy flags after a memory-exhaustion incident. Local and Actions gates must also assert Supabase CAPTCHA forwarding, configured email confirmation, and `ADMIN_EMAIL`-only administrative authority.

### 1. Shift-Left Local Verification FIRST (Multi-Language Stack & Dual Git Hooks)

- **MANDATORY BEFORE CREATING ANY PULL REQUEST OR COMMITTING CODE**:
  - **Pre-Commit Gatekeeper Hook**: Local commits are enforced by `.githooks/pre-commit` (with `core.hooksPath=.githooks`), which runs Sonar secrets scanning followed by `bash run_checks.sh --fast`. This differential gate checks modified files with Ruff AST formatting/linting, ESLint, strict Sonar-aligned YAML linting, Hadolint Dockerfile audits, markdownlint, SurrealQL validation, Bandit, Semgrep, AST-Grep regex rules, Python Cognitive Complexity (scripts/check_code_quality.py <= 15), and ShellCheck where applicable. The AST-Grep rules block recurring regex complexity and backtracking findings before a push; new suppressions require a precise rule ID, with Semgrep and SonarQube suppressions also requiring a reason.
  - **Pre-Push Gatekeeper Hook**: Local pushes are enforced by `.githooks/pre-push` (with `core.hooksPath=.githooks`), running Sonar secrets scanning followed by the full verification suite across Python (`ruff`, `mypy`, `bandit`), JavaScript (`eslint`), YAML (`yamllint`), Docker (`hadolint`), SurrealQL (`surreal validate`), and AST pattern rules (`ast-grep`).
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

### 4. Automated Cloud SAST & Quality Gate (SonarCloud & Semgrep)

- Once `run_checks.sh` passes locally and the user approves remote pushing, push to `origin main` or open a PR.
- The 3-phase GitHub Actions pipeline will automatically trigger:
     1. Pre-Scan Validation — blocking Hadolint + shell script syntax check
     2. Shift-Left Security — runs Ruff, ESLint, strict Sonar-aligned YAML linting, AST-Grep regex rules, Semgrep, Bandit, and tests with coverage.
  - GitHub-managed CodeQL and the SonarCloud GitHub integration publish code-scanning findings. CI publishes blocking Semgrep SARIF findings, while Dependency Review rejects newly introduced moderate-or-higher vulnerable dependencies on pull requests.
  - CI blocks on zizmor GitHub Actions security findings. OpenSSF Scorecard publishes scheduled, advisory supply-chain posture findings to GitHub code scanning.
  - Workflow-security scanners must check out the repository before auditing it so an empty runner workspace cannot produce a false failure.
  - Cloud Build steps that source computed metadata must use Kaniko's BusyBox-enabled debug image pinned by immutable digest; the standard executor image has no shell. Use a registry-backed Kaniko cache and bounded image, filesystem, and push retries.
  - Cloud Build may construct the immutable image in parallel, but it must wait for the successful GitHub SonarCloud check on the exact commit SHA before either Cloud Run deployment. Manual builds must provide a previously verified commit SHA.
  - Cloud Build must generate an SPDX SBOM with pinned open-source Syft and attach it to the immutable Artifact Registry image. Do not enable billed Artifact Analysis scanning solely to generate SBOMs.
  - Before deployment, Cloud Build must verify the Pulumi-managed media bucket, the dedicated runtime service account, and that account's `roles/storage.objectAdmin` binding. These checks are blocking and must use the same dynamic project-derived names as Pulumi.
  - Keep CI security scanners such as Semgrep in an isolated virtual environment when their dependency graph differs from the application runtime; install application checks from `requirements-dev.txt` and keep the scan blocking.
  - Pin every GitHub Action to a full commit SHA, retaining the reviewed release tag only as an adjacent comment.

### 4.1 Native GitHub Auto-Merge & PR Creation Protocol

- The repository is configured with `allow_auto_merge = true` and `allow_update_branch = true` alongside strict required status checks (`required_status_checks.strict = true`).
- The auto-sync workflow must set `GH_REPO` to the current repository before invoking `gh`, compare each PR base SHA with its target branch, and fail visibly on refresh, conflict, or auto-merge errors. A green lifecycle job is evidence that it acted, not merely that its shell script exited.
- When the user commands opening a Pull Request, use the GitHub CLI with `--auto --squash --delete-branch`:

  ```bash
  gh pr create --title "<type>(<scope>): <description>" --body "<summary>" --auto --squash --delete-branch
  ```

- GitHub will automatically keep the PR branch synchronized with `main` and execute the squash-merge as soon as all checks pass.

### 5. Local-First Review & No Unsolicited Remote Pushing

- **DO NOT AUTOMATICALLY PUSH INCREMENTAL EDITS TO REMOTE GITHUB**:
     Keep all commits and code modifications local for explicit user review (`git status`, `git diff`). Never execute `git push` unless specifically requested by the user.

### 6. Fork & Upstream PR Safety Guard

- **DO NOT TRIGGER OR TARGET UPSTREAM ORIGIN PARENT REPOSITORIES ON FORKS**:
     All PRs, branches, and commits MUST target `origin` directly.

### 7. Release, GCP, and Agent Hand-off

- Compute the release version before SonarCloud analysis and Cloud Build; never
  manually edit a release version or deploy a `latest` fallback.
- Treat the public `/release/` response as the deployed release authority. Cloud
  Build must verify its version and commit SHA after deployment; README must not pin
  computed release values that can drift after squash merges.
- Cloud Build trigger checkouts are shallow. Unshallow them before deriving the
  commit-count patch so SonarCloud, Cloud Run, image tags, and the UI use one version.
- Supabase CAPTCHA tokens must be sent in GoTrue `gotrue_meta_security`; require
  Turnstile before credential dispatch and grant admin only through `ADMIN_EMAIL`.
  Supabase app metadata must not grant Django superuser. Never auto-promote the first user.
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
- Treat the GitHub Actions summary as the actionable SonarCloud hand-off. Keep
  failures blocking so Jules can address scoped issues from PR checks or issues.
- Public GitHub repositories run native SonarCloud analysis across all pull requests
  and branch pushes, providing live PR decoration and quality gate evaluation.
- Use `scripts/gcp-diagnostics.sh` only for read-only Cloud Run diagnosis. Do
  not reintroduce secret-retrieval or imperative provisioning scripts.
- Use Pulumi for new or rebuilt GCP environments. Import and preview existing
  infrastructure before applying changes.

### 8. Proper Tooling Utilization & Multi-MCP Triage Protocols

- **Mandatory Tooling & Multi-MCP Integration for Fast Triage**:
  - **Sequential Thinking MCP (`sequential-thinking`)**: Leverage structured step-by-step reasoning for complex architectural refactoring, root-cause analysis of subtle bugs, multi-variable dependency resolution, and deep verification planning.
  - **SonarQube / SonarCloud MCP (`sonarqube`)**: Proactively query live quality gate status, rule violations, cognitive complexity hotspots, and security findings (`search_sonar_issues_in_projects`, `get_project_quality_gate_status`, `show_security_hotspot`, `analyze_code_snippet`) during investigation and verification turns.
  - **Google Cloud Logging & Monitoring MCP (`google-cloud-logging`, `google-cloud-monitoring`, `cloudrun`)**: Triage Cloud Run deployment health, container logs, worker queue execution traces, and performance metric timeseries (`list_log_entries`, `get_service_log`, `list_timeseries`) directly without manual console lookup.
  - **Composio Automation MCP (`remote-composio`)**: Inspect real-time production error traces, Sentry issues, alert workflows, and third-party integrations across managed cloud toolkits (`COMPOSIO_SEARCH_TOOLS`, `COMPOSIO_MULTI_EXECUTE_TOOL`).
  - **Chrome DevTools MCP (`chrome-devtools-mcp`)**: Audit UI/UX regressions, Lighthouse Core Web Vitals, accessibility tree standards (`a11y-debugging`), browser console errors (`list_console_messages`), and network payload bottlenecks (`list_network_requests`).
  - **Google Developer Knowledge MCP (`google-developer-knowledge`)**: Fetch authoritative documentation and architecture guidelines for Cloud Run, IAM keyless authentication, and Vertex AI integrations.
  - Always prioritize deep programmatic inspection, sequential reasoning, and MCP-grounded evidence over speculative diagnostics.
- **Batch PR & Local Verification Policy**:
  - Accumulate fixes and improvements locally on working branches; avoid opening small, incremental PRs.
  - Verify full passes locally via `bash run_checks.sh` and only bundle cohesive batches into a single PR when all related issues in a turn or feature milestone are completely resolved.

---

## 🛡️ Primary DevSecOps & Architectural Standards

1. **Formal Standardized Batch Export Naming**:
   - All batch exports must follow the formal sequential 3-digit index pattern: `<3-DIGIT_INDEX>_<CLEAN_TITLE_SLUG>.md` (e.g., `001_enterprise_legal_contract.md`).

2. **Dynamic GCP & Secret Manager Resolution**:
   - Never hardcode GCP project IDs, project numbers, or API keys (`OPENROUTER_API_KEY`) in committed manifests (`service.yaml`, `service-worker.yaml`, scripts, or database models). Sourced dynamically from environment variables or GCP Secret Manager.

3. **Dynamic Documentation & Zero Static Numbers**:
   - Avoid hardcoding static version strings, score numbers, test counts, or rule counts in Markdown text or tables. Use dynamic badges or auto-generated images (`scorecard.png`) to prevent stale documentation.

4. **SonarCloud Public Repository Architecture (`https://sonarcloud.io`)**:
   - Target Host: `https://sonarcloud.io` (Organization: `lucivskvn`, Project: `lucivskvn_AetherOmni`).
   - Zero local server footprint: Reclaims local RAM and CPU cycles with automated PR decoration.

5. **SurrealDB Native Transactions, Schema Validation & Async Safety**:
   - Execute budget caps and atomic counters using SurrealDB native `BEGIN TRANSACTION ... COMMIT TRANSACTION;` blocks.
   - Validate all `.surql` files with `surreal validate` before committing. Integrated into Phase 2 of `run_checks.sh` and the fast `--fast` differential pass.

- Use `concurrent.futures.ThreadPoolExecutor` worker dispatch in sync/async boundaries instead of monkeypatching event loops with `nest_asyncio`.
- Set `SURREAL_EXECUTOR_WORKERS` per deployment when tuning bounded SurrealDB RPC concurrency; it must be a positive integer.
- Keep `scripts/check_reliability_contracts.py` blocking in local and CI gates. It protects task ownership/cancellation, recovery-token lifetime, YAML export escaping, and dashboard polling lifecycle against regressions found by review tools.

1. **Multi-Language Code Quality & Eco-Design**:
   - Python: Target Python 3.14+ across Docker, CI, local checks, and SonarQube, with `ruff` (target-version = py314, PEP 758 syntax), `mypy`, and `bandit`.
   - JavaScript: ESLint 10 with SonarQube JS conventions (`?.` optional chaining, global scope checks, complexity ≤ 15).
   - SurrealQL: `surreal validate` enforces `.surql` schema syntax on every pipeline run.
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings.

2. **Dual-Tier State Architecture & Ephemeral Cloud Run Persistence**:
   - Relational Identity & Auth: Supabase PostgreSQL (`SUPABASE_DATABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`) securely manages users, sessions, and administrative privileges.
   - High-Throughput & Vector Engine: Remote SurrealDB (`SURREAL_URL` WebSocket RPC) manages documents, vector chunks (HNSW 768 cosine), tokenized prompt caches (`context_cache`), distributed atomic sliding-window quotas (`rate_limits`), user memories (`user_memories`), and compliance audit logs.
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
   Release versions must be computed as MAJOR.MINOR.PATCH before SonarCloud analysis and deployment.
7. Standardized Model & Auth Naming: Use Gemini 2.5 Flash / 2.5 Flash-Lite for the stable Vertex v1 production path; preview-model adoption requires an availability check. Refer to Vertex AI Vision, GCP Secret Manager, and Supabase Auth Platform (Supabase Cloud).
```

<!-- sonar:begin:codex-secrets-on-read -->
## SonarQube secrets scanning for files protocol

Before reading any file in this workspace, scan it for secrets with the deterministic scanner:

```bash
sonar analyze secrets <path/to/file>
```

If the command reports that the file contains a secret, **do not read the file**. Instead:

1. Inform the user that the file appears to contain a secret or credential and that reading it would expose the value in chat history, logs, and any downstream telemetry.
2. Advise them to rotate the leaked credential at its source of truth and remove it from the file.
3. Do not proceed with the original request until the secret has been removed.
<!-- sonar:end:codex-secrets-on-read -->
