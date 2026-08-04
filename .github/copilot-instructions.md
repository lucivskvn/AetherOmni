# 🤖 Unified Multi-AI & AI Coding Assistant Instructions

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Snyk Security, CodeRabbit AI Reviewer.

---

## 🎯 Primary Guidelines & DevSecOps Mandates

1. **Shift-Left Local Verification**:
   - Before submitting changes, execute local quality gate checks via `scripts/verify-pipeline.sh`.
   - Must achieve **0 Blocker / High Security Vulnerabilities** across Hadolint, Bandit, AST-Grep, and SonarQube MQR.

2. **Remote Self-Hosted SonarQube Architecture (`https://sonarqube.fainko.cloud`)**:
   - Target Host: `https://sonarqube.fainko.cloud` (Coolify Cloudflare Tunnel).
   - Zero local server footprint: Reclaims local RAM and CPU cycles.
   - GitHub Actions Integration: Automated SAST scans trigger on every `push` and `pull_request` using `SONAR_TOKEN`.

3. **Hardware & Operating System Stability**:
   - Battery-less AC Operation: Disables systemd kernel suspend/hibernate (`disable-suspend.conf`) to prevent ACPI EC deadlocks.
   - Nvidia Dynamic Power Management: Sets `options nvidia NVreg_DynamicPowerManagement=0x00` to prevent `D3cold` SBIOS power state lockups during IDE and browsing sessions.
   - Thread Concurrency Cap: Caps SonarScanner CLI workers to `-Dsonar.threads=4` to prevent CPU thrashing.

4. **Code Quality & Eco-Design**:
   - Adhere to Creedengo Eco-Design rules (low energy consumption, optimal memory management).
   - Zero dead code, unused imports, or non-UTF-8 binary encodings.
