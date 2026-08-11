---
name: aetheromni-delivery
description: Safely deliver, diagnose, or plan AetherOmni changes across local checks, GitHub Actions, SonarQube, Supabase Auth, Cloud Build, Cloud Run, release versions, and planned Sentry/Pulumi work. Use for any AetherOmni implementation, CI failure, security finding, deployment, production incident, or roadmap task.
---

# AetherOmni Delivery

Use the repository root as the working directory. Read `AGENTS.md` first; it
is the cross-agent source of truth.

## Delivery flow

1. Inspect the worktree and relevant code before editing. Preserve unrelated changes.
2. Run `bash run_checks.sh --fast` for documentation/chore-only work; run
   `bash run_checks.sh` for source, workflow, deployment, or security changes.
3. Keep commits local unless the user explicitly authorizes a push. Target
   `origin`, never an upstream parent.
4. On GitHub, treat the Actions summary as the agent hand-off: resolve the
   failing job and logs before changing code. Sonar failures must be visible as
   a failed check, not only in the Sonar dashboard.
5. Compute the release version before Sonar analysis and Cloud Build. Do not
   manually edit the release version or restore a `latest` deployment fallback.
6. Diagnose Cloud Run with `bash scripts/gcp-diagnostics.sh --service all`.
   It is read-only; never retrieve or print secret values.

## Security and platform boundaries

- Use Supabase for interactive authentication. Verify OAuth, Passkey, session,
  redirect, and CSRF changes with focused tests plus the full gate.
- Keep application credentials in GCP Secret Manager. Do not add credentials to
  `.env` examples, GitHub Actions output, logs, or tracked manifests.
- Use Pulumi for any new or rebuilt GCP environment. Import and preview first;
  do not replace live Cloud Run or Secret Manager resources with an imperative
  script.
- Treat Sentry as a planned release-observability integration: use the same
  computed release version and deploy metadata, with DSN stored as a secret.

## MVP priority

Prioritize functional login, safe document processing, reproducible releases,
actionable CI/security findings, and production observability before enterprise
RBAC, graph RAG, or autonomous agents.
