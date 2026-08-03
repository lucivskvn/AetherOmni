# 🤖 Unified Multi-AI & AI Coding Assistant Instructions

> **Compatible AI Tooling Standards**: Google Antigravity 2.0 / IDE & CLI, Jules AI, OpenAI Codex, Claude Code, Amazon Kiro, Semgrep SAST, Snyk Security, CodeRabbit AI Reviewer.

---

## 🎯 Primary Guidelines & DevSecOps Mandates

1. **Shift-Left Local Verification**:
   - Before submitting changes, execute local quality gate checks via ======================================================================
🛡️ STARTING DEVSECOPS PRE-PRODUCTION QUALITY & SECURITY PIPELINE
   Target Directory: /home/elang
   Timestamp: Mon Aug  3 09:14:53 AM WITA 2026
======================================================================

🔄 [STEP 0/7] Fetching and Pulling Remote Branch Changes...
   ℹ️ Git repository initialized or no commits yet. Skipping git pull.

🐳 [LAYER 1/7] Running Container & Dockerfile Hardening Check...
   ℹ️ No Dockerfile found in project root.

🔍 [LAYER 2/7] Running Linter, Auto-Fixers & AST Analysis...

🏗️ [LAYER 3/7] Running Desloppify Structural Scan...
   ⚠️ Desloppify not found or .desloppify uninitialized. Skipping structural scan.

🛡️ [LAYER 4/7] Running SAST & Security Scans...
   ℹ️ Semgrep engine active inside SonarQube SAST pipeline.

🧪 [LAYER 5/7] Executing Unit Tests & Generating Coverage XML...

📊 [LAYER 6/7] Submitting to SonarQube MQR Server...
01:14:54.442 INFO  Scanner configuration file: /opt/sonar-scanner/conf/sonar-scanner.properties
01:14:54.445 INFO  Project root configuration file: NONE
01:14:54.457 INFO  SonarScanner CLI 8.0.1.6346
01:14:54.461 INFO  Linux 7.0.0-28-generic amd64
01:14:55.275 INFO  Communicating with SonarQube Community Build 26.7.0.124771
01:14:55.276 INFO  JRE provisioning: os[linux], arch[x86_64]
01:14:57.270 INFO  Starting SonarScanner Engine...
01:14:57.270 INFO  Java 21.0.9 Eclipse Adoptium (64-bit)
01:14:58.319 INFO  Load global settings
01:14:58.396 INFO  Load global settings (done) | time=76ms
01:14:58.399 INFO  Server id: 243B8A4D-AZ-7UJHc41khpdXQPnS6
01:14:58.410 INFO  Loading required plugins
01:14:58.410 INFO  Load plugins index
01:14:58.424 INFO  Load plugins index (done) | time=15ms
01:14:58.424 INFO  Load/download plugins
01:14:58.455 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache8004048551905397972.tmp to /opt/sonar-scanner/.sonar/cache/9621b3acc1db2f6f8504fa95a774eb9e/sonar-cayc-plugin.jar
01:14:58.456 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.523 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache13480309852918809741.tmp to /opt/sonar-scanner/.sonar/cache/b07467cd5050923a252771867828274e/sonar-dependencycheck-plugin.jar
01:14:58.523 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.673 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache18206820917851118454.tmp to /opt/sonar-scanner/.sonar/cache/ef850d5596ac402a564dcccc0573de23/sonar-iac-plugin.jar
01:14:58.673 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.758 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache12004032102582302608.tmp to /opt/sonar-scanner/.sonar/cache/7d0cd2ed0149bab9d0b92592ca3d11da/sonar-hadolint-plugin.jar
01:14:58.759 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.810 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache13439279825884662533.tmp to /opt/sonar-scanner/.sonar/cache/c3431b45b5c9f944661f0079c4bb99ff/sonar-text-plugin.jar
01:14:58.810 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.849 WARN  Unable to rename /opt/sonar-scanner/.sonar/_tmp/fileCache6075815001391188898.tmp to /opt/sonar-scanner/.sonar/cache/4c4570e8374bcc74d4cf61e56d1540fd/sonar-yaml-plugin.jar
01:14:58.849 WARN  A copy/delete will be tempted but with no guarantee of atomicity
01:14:58.857 INFO  Load/download plugins (done) | time=432ms
01:14:59.244 INFO  Process project properties
01:14:59.322 INFO  EXECUTION FAILURE
01:14:59.323 INFO  Total time: 4.884s

🐙 [LAYER 7/7] Checking GitHub CLI Integration & PR Status...
   ℹ️ No remote Git repository origin configured. Skipping GH PR status.

======================================================================
❌ DEVSECOPS GATE FAILED: Issues Detected. Check Logs Above..
   - Code changes MUST achieve **0 Blocker / 0 Critical Security Vulnerabilities** in SonarQube.
   - Maintain a **100/100 Objective Cleanliness Score** in Desloppify.

2. **Code Integrity & Non-Guess Directive**:
   - **NEVER** guess function signatures, API schemas, or variable names. Always view and inspect authoritative source files.
   - Preserves exact existing public API contracts and type parameters.
   - Format Python using  and TypeScript/JS using  / .

3. **Tooling & Multi-Engine Compatibility**:
   - **SonarQube & Eco-Design**: Enforce Creedengo Green Code rules (GCI29, GCI11, GCI12) to minimize CPU, memory, and DOM footprint.
   - **Container Security**: Enforce Hadolint Dockerfile hardening standards ().
   - **CodeRabbit AI**: High-level Assertive review guidelines focusing on type safety, non-empty assertions in tests, and zero bare  clauses.
   - **Semgrep & Snyk**: Keep security annotations aligned with OWASP Top 10 and CWE Top 25 standards.
