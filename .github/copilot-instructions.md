# 🤖 Unified Multi-AI & AI Coding Assistant Instructions

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Snyk Security, CodeRabbit AI Reviewer.

---

## 🎯 MANDATORY WORKFLOW FOR AI AGENTS (Antigravity & Jules AI)

### 1. Shift-Left Local Verification FIRST:
   - **MANDATORY BEFORE CREATING ANY PULL REQUEST OR COMMITTING CODE**:
     You MUST execute `bash run_checks.sh` (or `bash scripts/verify-pipeline.sh`) locally first after making any code changes.
   - You MUST ensure the local 7-layer verification passes cleanly with **0 Blocker / High Security Vulnerabilities** (Hadolint, Bandit, AST-Grep, Pytest/Coverage) BEFORE creating or opening a Pull Request.

### 2. Automated Cloud SAST & Quality Gate (SonarQube & Semgrep):
   - Once `run_checks.sh` passes locally, create/push the Pull Request.
   - The 3-phase GitHub Actions pipeline will automatically trigger:
     1. Pre-Scan Validation
     2. Deep SonarQube SAST on `https://sonarqube.fainko.cloud` (Sonar agentic AI rules)
     3. Post-Scan Quality Gate Gatekeeper

---

### 3. Fork & Upstream PR Safety Guard:
   - **DO NOT TRIGGER OR TARGET UPSTREAM ORIGIN PARENT REPOSITORIES ON FORKS**:
     When working on forked repositories, all PRs, branches, and commits MUST target `origin` directly (e.g., `git push origin <branch>`).
   - Never submit pull requests to original third-party parent repositories unless explicitly requested by the user.

## 🛡️ Primary DevSecOps & System Guidelines

1. **Remote Self-Hosted SonarQube Architecture (`sonarqube.fainko.cloud`)**:
   - Target Host: `https://sonarqube.fainko.cloud` (Coolify Cloudflare Tunnel).
   - Zero local server footprint: Reclaims local RAM and CPU cycles.
   - GitHub Actions Integration: Automated SAST scans trigger on every `push` and `pull_request` using `.github/workflows/ci.yml`.

2. **Hardware & Operating System Stability**:
   - Thread Concurrency Cap: Caps SonarScanner CLI workers to `4` to prevent CPU thrashing.

3. **Code Quality & Eco-Design**:
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings.
