# KORDA

KORDA is a secure knowledge workspace for turning document collections into searchable, grounded answers. Upload source material, follow its processing status, and retrieve answers with links back to the documents that support them.

[![CI](https://github.com/lucivskvn/AetherOmni/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/lucivskvn/AetherOmni/actions/workflows/ci.yml)
[![Quality Gate](https://sonarcloud.io/api/project_badges/measure?project=lucivskvn_AetherOmni&metric=alert_status)](https://sonarcloud.io/summary/new_code?id=lucivskvn_AetherOmni)
[![License](https://img.shields.io/badge/License-AGPL--3.0-blue.svg)](LICENSE)

## What it does

- Ingests PDF, office documents, images, text, structured data, and ZIP batches.
- Tracks asynchronous extraction, curation, embedding, and cancellation safely.
- Provides tenant-scoped semantic search with grounded source references.
- Exports curated document bundles and dataset-ready formats.
- Applies configurable spend limits, audit logging, and production-grade authentication controls.

## How people use KORDA

1. Sign in and upload one or more source files from the dashboard.
2. Follow each document through queued, processing, completed, or failed states.
3. Open a document to review and refine its extracted content, or retry and cancel work when needed.
4. Ask a question in Semantic Spotlight Search and inspect the grounded source material returned with the answer.
5. Select documents and export a Markdown archive, SFT JSONL, SQLite FTS database, or CSV metadata summary.

The dashboard keeps document activity, monthly AI spend, processing status, and semantic search in the same workspace. Administrators additionally have access to configuration, audit history, and the deployment controller.

## Detailed use cases

### Compliance, legal, and policy research

Bring contracts, policy manuals, regulatory guidance, and internal procedures into one searchable workspace. KORDA keeps the original document relationship visible through grounded source references, so a reviewer can move from an answer back to the supporting material instead of treating generated text as the only record. Curated exports provide a portable review or archive package when a matter needs to leave the workspace.

### Research libraries and long-form source material

Researchers can ingest papers, reports, scans, notes, and structured datasets in batches, then ask focused questions across the collection. Document-level editing helps correct or enrich extracted material before it is used for retrieval. The JSONL and SQLite exports are useful when a project needs a reproducible hand-off to an offline analysis or evaluation workflow.

### Operational knowledge and incident follow-up

For runbooks, support material, post-incident notes, and architecture documents, the dashboard gives a small team a live view of what is still processing, what failed, and what can be retried. Semantic search is designed for finding relevant material quickly while retaining the references a responder needs to validate an answer.

### AI and data preparation

KORDA is useful before model evaluation or dataset curation: it transforms mixed document input into reviewed, structured output with provenance retained in the workspace. Teams can export selected documents as an SFT JSONL dataset, a full-text SQLite database, a Markdown archive, or CSV metadata rather than building separate conversion scripts for each hand-off.

## Typical workflow

| Step | What happens | What the user can do |
| --- | --- | --- |
| Upload | The dashboard validates accepted files and records the submission. | Add individual files or batches, then see them immediately in the library. |
| Process | A queued worker extracts, curates, and indexes document content. | Follow status, cancel work that is no longer needed, or retry a failed document. |
| Review | The document detail view exposes the resulting content and metadata. | Refine a document before it becomes part of a downstream export or search workflow. |
| Retrieve | Hybrid search locates relevant content and produces a grounded response. | Ask a question, read the answer, and inspect its returned source context. |
| Export | Selected content is packaged for a specific downstream use. | Download a Markdown archive, SFT JSONL, SQLite FTS database, or CSV summary. |

## Current capabilities

| Area | Available now |
| --- | --- |
| Document intake | PDF, office files, images, text, JSON, CSV, spreadsheets, and recursive ZIP batches. |
| Processing | Queued worker execution with durable task identity, cancellation fencing, retry controls, and safe failure handling. |
| Knowledge retrieval | Tenant-scoped hybrid retrieval, grounded source references, and streaming answers with a safe JSON fallback. |
| Curation and export | Editable document detail views plus Markdown ZIP, SFT JSONL, SQLite FTS, and CSV exports. |
| Governance | Spend caps, immutable audit records, user-scoped data access, and administrator-only operational controls. |
| Delivery | Private Cloud Run worker, Cloud Tasks OIDC delivery, Pulumi-managed GCP resources, and immutable image release verification. |

## Current milestone

**Reliable knowledge-workspace MVP — active.** The current focus is a dependable path from authenticated upload to worker processing, grounded retrieval, and portable export. Reliability work now includes task-identity fencing, recoverable storage cleanup, user-scoped dashboard status refreshes, production-only distributed controls, and release verification before deployment.

## Roadmap and status

| Initiative | Status | Outcome |
| --- | --- | --- |
| Reliable knowledge-workspace MVP | **Active** | Complete the dependable upload, processing, retrieval, and export path before expanding collaboration features. |
| Task identity, cancellation, and recovery hardening | **Delivered** | Prevent stale or cancelled work from writing results and retain recoverable document state on storage failures. |
| User-scoped dashboard refresh and streamed search fallback | **Delivered** | Keep status views accurate and preserve usable retrieval when streaming is unavailable. |
| Saved searches and document collections | **Proposed** | Preserve recurring research views and reuse well-defined source sets. |
| Organization roles and shared workspaces | **Proposed** | Add explicit membership and role boundaries before introducing broader collaboration. |
| User-facing processing explanations | **Proposed** | Show actionable failure reasons, retry guidance, and worker progress milestones alongside each document. |
| Feedback and evaluation workflow | **Proposed** | Capture answer usefulness and citation quality against real team tasks. |
| Retention and export lifecycle controls | **Proposed** | Give administrators scheduled retention, export history, and recovery visibility without weakening deletion guarantees. |

Proposed items are intentionally not represented as shipped capabilities. Their order reflects the recommended sequence: preserve individual research workflows first, establish collaboration boundaries second, then add richer operational and evaluation tooling.

## Architecture

KORDA uses Django for the web workspace, Supabase Auth and PostgreSQL for identity, and SurrealDB for high-throughput document and vector operations. Cloud Tasks dispatches ingestion to a private Cloud Run worker. The production deployment uses Pulumi-managed GCP resources and immutable container images.

```text
Browser → Django workspace → Cloud Tasks → private worker
                         ↘              ↙
                     Supabase       SurrealDB
```

The browser dashboard uses complete, authenticated status snapshots with Realtime hints. This keeps the interface correct even if a transient notification is missed, while the worker remains the only production path that performs document processing.

## Local development

KORDA targets the Python version declared in `pyproject.toml` and uses Node.js for the browser test suite.

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements-dev.txt
npm ci
export SURREALDB_OFFLINE=True
python manage.py migrate
python manage.py runserver
```

For offline development, set `SURREALDB_OFFLINE=True`; this uses the local SQLite database `db.sqlite3` and does not require `DATABASE_URL` or SurrealDB credentials. Production configuration is documented in the [GCP deployment guide](docs/gcp_deployment_guide.md).

## Quality and security

Run the complete local gate before committing source, UI, workflow, or infrastructure changes:

```bash
bash run_checks.sh
```

`bash run_checks.sh` is the local lint, type, test, and security gate. GitHub Actions adds Dependency Review, CodeQL, Semgrep SARIF, and zizmor; SonarCloud runs its separate hosted analysis. Cloud Build requires commit-scoped passing SonarCloud, CodeQL, and Semgrep results before deployment. CI installs application checks from the committed `uv.lock`, keeps Semgrep isolated, and checks out the repository before workflow-security analysis. Production images receive an SPDX SBOM generated with Syft and attached to the same resolved Artifact Registry digest.

## Contributing

Keep changes focused, preserve tenant boundaries and task-identity fencing, and run the relevant verification gate. For GCP changes, use the existing Pulumi project and preview infrastructure changes before applying them.

## License

KORDA is licensed under [AGPL-3.0](LICENSE).
