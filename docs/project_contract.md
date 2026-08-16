# AetherOmni Project Contract

## Purpose and status

AetherOmni is an MVP for turning authenticated document uploads into searchable,
curated knowledge. This contract defines what the repository currently supports,
the conditions required for production operation, and the work that remains
planned. It supersedes contradictory capability or compliance claims elsewhere.

## Supported user journey

1. A user authenticates with Supabase Auth using password, registration, or
   recovery flows. Turnstile is enforced when configured.
2. An authenticated user uploads a supported file within the server-side size
   limit.
3. The web service creates an owned document record and queues processing for
   the Cloud Run worker in production.
4. The worker extracts content, attempts AI-assisted refinement, generates
   embeddings, and writes retrieval projections to SurrealDB.
5. The user can review, export, and query documents that their identity is
   authorized to access.

An accepted upload can still fail processing because a parser, storage service,
SurrealDB, Cloud Tasks, or model provider is unavailable. The product must show
the failure and preserve a retryable document state; it does not promise every
file will be processed successfully.

## Identity and authorization

- Supabase Auth is the interactive identity provider.
- Django maintains a corresponding local user/session representation for the
  application and relational models.
- The Supabase subject UUID is used as the production actor identifier for
  tenant-scoped SurrealDB records and request rate-limit keys. Django user IDs
  remain relational implementation details.
- Server-side ownership checks are required for document views, deletion,
  export, task processing, cache reads, and RAG retrieval.
- GitHub OAuth requires Supabase provider configuration and redirect allowlists;
  it is not a guaranteed enabled login method. Passkey/WebAuthn support is not
  implemented.
- Administrator authority is granted only by configured server-side policy, not
  by first-user registration.

## Data ownership and storage

| Data | Authoritative store | Contract |
| --- | --- | --- |
| Django users, sessions, settings, audit records, spend logs, relational document records | PostgreSQL in production; SQLite only in offline/test mode | Production requires `DATABASE_URL` from GCP Secret Manager. |
| Raw production file assets | Google Cloud Storage when configured | The application must not depend on Cloud Run local disk for durable user data. |
| Tenant-scoped document projections, chunks, embeddings, semantic caches, and user memory | SurrealDB | Data must be queried and written with the authenticated actor boundary. |
| Credentials and deployment configuration | GCP Secret Manager | Never commit, render, retrieve, or log secret values. |

The running production revision is not compliant with this storage contract
until `SUPABASE_DATABASE_URL` exists and a new Cloud Run revision binds it as
`DATABASE_URL`. Until then, Cloud Run can fall back to ephemeral SQLite state.

## Processing and retrieval

- Production ingestion is asynchronous: the web service enqueues only to
  `WORKER_URL`, and Cloud Tasks authenticates task calls.
- The worker uses stable Gemini 2.5 Flash and Flash-Lite defaults through
  Vertex AI or AI Studio pathways. Provider failures are expected operational
  errors, not hidden successes.
- Retrieval uses authorized document scope, dense HNSW search, BM25 search, and
  reciprocal-rank fusion when SurrealDB is healthy and indexed.
- Generated answers must carry source links, but grounding reduces risk rather
  than guaranteeing factual correctness or eliminating hallucinations.
- Hash and embedding caches are optimizations. They may avoid work and cost;
  they are not a financial guarantee.

## Security and audit boundary

- AuditLog model methods prevent ordinary application updates and deletes unless
  an explicit internal override is supplied.
- This is not a database trigger, WORM store, independently verifiable ledger,
  SOC 2 certification, or ISO certification. Such claims require separate
  controls, evidence, review, and operational ownership.
- Uploaded content, extracted text, prompts, embeddings, answers, and audit
  metadata are sensitive and must not be logged unnecessarily or crossed between
  tenants.

## Delivery contract

1. Run the local gate before commit or PR work; run the full gate for source,
   security, workflow, or infrastructure changes.
2. GitHub PR checks provide blocking shift-left security and quality feedback.
   SonarQube Community Edition analyzes `main`; PR baseline output is context,
   not PR analysis.
3. Cloud Build computes release metadata from the verified commit, waits for its
   successful mainline quality gate, checks only the existence of the required
   database secret, deploys worker first, then deploys web.
4. Deployment must stop on any failed prerequisite. A release is not complete
   until Cloud Run startup, authentication, upload dispatch, worker processing,
   and persistence smoke tests have passed.

## Explicit non-goals and planned work

- Passkey/WebAuthn login, enterprise RBAC, and organizational tenancy.
- Pulumi-managed infrastructure reconciliation.
- Retrieval quality, latency, cost, and model-availability service-level
  objectives.
- Compliance certification or database-enforced immutable audit storage.

Changes that alter this contract must update this document, the README, and the
relevant deployment or agent guidance in the same change.
