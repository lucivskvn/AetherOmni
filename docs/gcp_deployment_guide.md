# Google Cloud Run Production Deployment Guide

This guide describes how to provision, configure, build, and deploy the **AetherOmni** application to production on **Google Cloud Run**, utilizing Supabase PostgreSQL for Django relational state, **SurrealDB** for vector storage/RAG caches, **Google Cloud Tasks** for background task queuing, Google Cloud Storage, and Google Secret Manager. SQLite is restricted to explicit offline/test use.

---

## 1. Production Architecture Overview

The production system consists of:

1. **Cloud Run Service (`aether-web`)**: Handles user HTTP traffic and serves dashboard/login pages. Production users, sessions, audit logs, spend history, and settings persist in Supabase PostgreSQL; document ownership and retrieval use stable Supabase Auth subject UUIDs in SurrealDB.
2. **Cloud Run Service (`aether-worker`)**: Dedicated worker instance that processes heavy background OCR, visual diagram processing, and RAG ingestion.
3. **Google Cloud Tasks Queue (`extractor-tasks`)**: Orchestrates background document processing. Tasks are dispatched from `web` to Cloud Tasks, which trigger HTTP POST callbacks targeting the `/internal/tasks/<task_name>/` endpoint on the `worker` service.
4. **Remote SurrealDB (rpc via WebSockets)**: Deployed as a secure, standalone service (at `wss://surrealdb.fainko.cloud/rpc`). It serves as the primary database store for all document metadata (`SourceDocument`), compliance audit logs (`AuditLog`), system settings (`SystemSettings`), vector chunk databases (`chunks`), and semantic search caches (`rag_cache`).
5. **Vertex AI & Gemini Multi-Modal Gateway**: Direct Application Default Credentials (ADC) access (`roles/aiplatform.user`) for stable Vertex v1 Gemini 2.5 Flash / Flash-Lite and Vertex AI Vision.
6. **Cloud Storage (GCS)**: Stores raw, uploaded PDF assets securely in GCP bucket (`GS_BUCKET_NAME`).
7. **Supabase Auth (GoTrue REST API)**: Handles user credentials, login, and registration securely on a self-hosted instance (at `https://supabase.fainko.cloud`).
8. **Secret Manager**: Securely stores environment credentials (`DJANGO_SECRET_KEY`, `SURREAL_URL`, `SURREAL_USER`, `SURREAL_PASS`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLIC_KEY`, `SUPABASE_DATABASE_URL`).

---

## 2. Provisioning and Deployment Policy

The legacy imperative provisioning script has been retired. It mixed local
`.env` secrets, broad IAM changes, infrastructure creation, and deployment in a
single command, making its results difficult to review and reproduce.

For the existing project, manage deployment through the reviewed Cloud Build
configuration in `infra/gcp/cloudbuild.yaml`. Infrastructure provisioning will
move to Pulumi before any new environment is created or an existing environment
is rebuilt. Until then, use the manual reference below and make narrowly scoped,
reviewed GCP changes.

---

## 3. Manual Provisioning Reference

Run these commands using the Google Cloud CLI (`gcloud`) or Cloud Shell.

### A. Set Environment Variables

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-southeast1" # Choose your preferred region
export SUFFIX="data-extractor"
export ARTIFACT_REGISTRY="data-extractor-repo"
export BUCKET_NAME="${PROJECT_ID}-media-${SUFFIX}"
export SERVICE_ACCOUNT="run-service-account@${PROJECT_ID}.iam.gserviceaccount.com"
export QUEUE_NAME="extractor-tasks"
```

### B. Enable GCP Services

```bash
gcloud services enable \
  run.googleapis.com \
  cloudtasks.googleapis.com \
  secretmanager.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com
```

### C. Create Artifact Registry

```bash
gcloud artifacts repositories create ${ARTIFACT_REGISTRY} \
  --repository-format=docker \
  --location=${REGION} \
  --description="Docker repository for AetherOmni"
```

### D. Create GCS Media Bucket

```bash
gcloud storage buckets create gs://${BUCKET_NAME} \
  --location=${REGION} \
  --uniform-bucket-level-access
```

### E. Create Google Cloud Tasks Queue

Create the Cloud Tasks queue to handle task rates:

```bash
gcloud tasks queues create ${QUEUE_NAME} \
  --location=${REGION} \
  --max-concurrent-tasks=10 \
  --max-attempts=5
```

---

## 4. Security & IAM Configuration

To follow security best practices (OWASP/SOC2 compliance), create a dedicated service account for Cloud Run services.

### A. Create Service Account

```bash
gcloud iam service-accounts create run-service-account \
  --description="Service account for running AetherOmni on Cloud Run" \
  --display-name="run-service-account"
```

### B. Grant Storage, Tasks, and Invoker Roles

```bash
# Grant Cloud Tasks Enqueuer permission (so web container can enqueue tasks)
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/cloudtasks.enqueuer"

# Grant Storage Admin access to the media bucket
gcloud storage buckets add-iam-policy-binding gs://${BUCKET_NAME} \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/storage.objectAdmin"

# Grant Cloud Run Invoker permission to service account (so Cloud Tasks can call web hooks securely)
gcloud projects add-iam-policy-binding ${PROJECT_ID} \
  --member="serviceAccount:${SERVICE_ACCOUNT}" \
  --role="roles/run.invoker"
```

---

## 4. Secret Manager Configuration

Store sensitive credentials in Secret Manager.

### A. Create Secrets

```bash
# 1. Django Secret Key
gcloud secrets create DJANGO_SECRET_KEY --replication-policy="automatic"
echo -n "YOUR_SECURE_SECRET_KEY" | gcloud secrets versions add DJANGO_SECRET_KEY --data-file=-

# 2. Gemini API Key
gcloud secrets create GEMINI_API_KEY --replication-policy="automatic"
echo -n "YOUR_GEMINI_API_KEY" | gcloud secrets versions add GEMINI_API_KEY --data-file=-

# 3. SurrealDB Credentials
gcloud secrets create SURREAL_URL --replication-policy="automatic"
echo -n "wss://surrealdb.fainko.cloud/rpc" | gcloud secrets versions add SURREAL_URL --data-file=-

gcloud secrets create SURREAL_USER --replication-policy="automatic"
echo -n "admin" | gcloud secrets versions add SURREAL_USER --data-file=-

gcloud secrets create SURREAL_PASS --replication-policy="automatic"
echo -n "YOUR_SURREALDB_PASSWORD" | gcloud secrets versions add SURREAL_PASS --data-file=-

# 4. Bootstrap contact
gcloud secrets create ADMIN_EMAIL --replication-policy="automatic"
echo -n "admin@example.com" | gcloud secrets versions add ADMIN_EMAIL --data-file=-


# 5. Supabase Auth Configuration (for user authentication)
gcloud secrets create SUPABASE_URL --replication-policy="automatic"
echo -n "https://supabase.fainko.cloud" | gcloud secrets versions add SUPABASE_URL --data-file=-

gcloud secrets create SUPABASE_PUBLIC_KEY --replication-policy="automatic"
echo -n "YOUR_SUPABASE_PUBLIC_KEY" | gcloud secrets versions add SUPABASE_PUBLIC_KEY --data-file=-
```

### B. Grant Secret Access to the Service Account

```bash
for secret in DJANGO_SECRET_KEY GEMINI_API_KEY SURREAL_URL SURREAL_USER SURREAL_PASS ADMIN_EMAIL SUPABASE_URL SUPABASE_PUBLIC_KEY; do
  gcloud secrets add-iam-policy-binding ${secret} \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/secretmanager.secretAccessor"
done
```

---

## 5. Build Container Images

Use Google Cloud Build to build and push your Docker container:

```bash
RELEASE_VERSION=$(python scripts/update_docs.py --print-version)
DEPLOY_COMMIT_SHA=$(git rev-parse HEAD)
gcloud builds submit --config infra/gcp/cloudbuild.yaml \
  --substitutions="_RELEASE_VERSION=${RELEASE_VERSION},_DEPLOY_COMMIT_SHA=${DEPLOY_COMMIT_SHA},_APP_URL=https://your-public-app.example"
```

Cloud Build resolves the current Cloud Run web and worker URLs before deployment,
sets `GOOGLE_CLOUD_PROJECT` for Vertex AI ADC, and passes `WORKER_URL` to both
services. The web process must only enqueue tasks; it must not run production
ingestion locally when the worker routing configuration is missing. Set `_APP_URL`
to the public origin that is allow-listed in Supabase Auth so confirmation and
recovery links return to the browser-facing login page.

Push-triggered Cloud Build checkouts are shallow by default. The reviewed build
configuration unshallows Git history before computing the commit-count patch so
its image tag and `RELEASE_VERSION` match the SonarQube analysis version. Manual
source uploads must pass `_RELEASE_VERSION` and `_DEPLOY_COMMIT_SHA` explicitly
as shown above. The latter must identify a commit with a successful mainline gate.

Cloud Build may construct and publish the immutable image while GitHub Actions
analyzes the same commit, but the web and worker deployment steps wait for that
exact SHA's successful SonarQube mainline check. Failed, cancelled, missing, or
timed-out checks stop deployment. The Actions log and summary both contain the
condition table, and failing metrics are emitted as annotations for Jules.

Supabase CAPTCHA-protected password, signup, and recovery calls forward the
Turnstile response inside GoTrue `gotrue_meta_security`. Admin authority comes
from the configured `ADMIN_EMAIL` or server-controlled Supabase app metadata;
the application never promotes the first authenticated user automatically.

Cloud Run deploys the worker in bounded on-demand mode by default. Cloud Tasks
wakes it for queued ingestion and it scales back to zero when idle; periodic
SurrealDB maintenance is disabled unless an explicit always-on operating mode is
selected. Web instances never start maintenance threads. Paid document deletion
stops if spend-ledger persistence fails.

---

## 6. SurrealDB Setup and HNSW Vector Indexes

### Automated Schema Bootstrap

The shell-free Python container entrypoint runs migrations, starts `init_surreal.py` as a bounded bootstrap process, and then `exec`s Gunicorn. The bootstrap waits for SurrealDB to become healthy, ensures the namespace and database are pre-defined, and imports the full schema without making the application command depend on a shell interpreter.

### Manual Schema Initialization

If you need to manually initialize or verify the SurrealDB schema, run the following queries (compatible with SurrealDB v3.x HNSW syntax).

> **SurrealQL Validation**: Before importing, validate the schema file locally using the official `surreal` CLI:
>
> ```bash
> # Install surreal CLI (Linux/macOS)
> curl -sSf https://install.surrealdb.com | sh
> # Validate schema syntax
> surreal validate schema.surql
> ```
>
> The `surreal validate` command enforces SurrealQL syntax correctness and runs automatically in `run_checks.sh`. Use the tool and interpreter versions declared by repository configuration with `requirements-dev.txt`; the gate rejects incompatible versions, keeping local checks aligned with Cloud Run and GitHub Actions.

GitHub Action dependencies are pinned to reviewed commit SHAs, preventing a mutable action tag from changing the deployment or quality-gate workflow unexpectedly.

```surrealql
-- ── 1. documents ─────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS documents SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid          ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS uploaded_by_id    ON documents TYPE option<string>;
DEFINE FIELD IF NOT EXISTS file              ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS original_filename ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS file_hash         ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS status            ON documents TYPE string;
DEFINE FIELD IF NOT EXISTS error_message     ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS language          ON documents TYPE string DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS author            ON documents TYPE string DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS title             ON documents TYPE string DEFAULT "Untitled";
DEFINE FIELD IF NOT EXISTS document_type     ON documents TYPE string DEFAULT "PDF";
DEFINE FIELD IF NOT EXISTS page_count        ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS raw_markdown      ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS refined_markdown  ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS yaml_metadata     ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS qa_dataset        ON documents TYPE array  DEFAULT [];
DEFINE FIELD IF NOT EXISTS input_tokens      ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS output_tokens     ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS cost_usd          ON documents TYPE float  DEFAULT 0.0;
DEFINE FIELD IF NOT EXISTS semantic_signature ON documents TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS retry_count       ON documents TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS expires_at        ON documents TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS publisher         ON documents TYPE option<string> DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS publication_year  ON documents TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS license_type      ON documents TYPE option<string> DEFAULT "Unknown";
DEFINE FIELD IF NOT EXISTS doi               ON documents TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS created_at        ON documents TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at        ON documents TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_documents_uuid ON documents FIELDS doc_uuid UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_documents_hash ON documents FIELDS file_hash;

-- ── 2. chunks ─────────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS chunks SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS doc_uuid        ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS chunk_index     ON chunks TYPE int;
DEFINE FIELD IF NOT EXISTS content         ON chunks TYPE string;
DEFINE FIELD IF NOT EXISTS token_count     ON chunks TYPE int    DEFAULT 0;
DEFINE FIELD IF NOT EXISTS language        ON chunks TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS page_number     ON chunks TYPE option<int> DEFAULT 1;
DEFINE FIELD IF NOT EXISTS chapter_title   ON chunks TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS anchor_id       ON chunks TYPE option<string> DEFAULT "";
DEFINE FIELD IF NOT EXISTS embedding       ON chunks TYPE array<float>;
DEFINE FIELD IF NOT EXISTS created_at      ON chunks TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_chunks_doc  ON chunks FIELDS doc_uuid;
DEFINE INDEX IF NOT EXISTS idx_chunks_hnsw ON chunks FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- ── 3. rag_cache ──────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS rag_cache SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS query_text      ON rag_cache TYPE string;
DEFINE FIELD IF NOT EXISTS query_embedding ON rag_cache TYPE array<float>;
DEFINE FIELD IF NOT EXISTS answer_text     ON rag_cache TYPE string;
DEFINE FIELD IF NOT EXISTS sources         ON rag_cache TYPE array<string>;
DEFINE FIELD IF NOT EXISTS user_id         ON rag_cache TYPE string;
DEFINE FIELD IF NOT EXISTS created_at      ON rag_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at      ON rag_cache TYPE datetime DEFAULT time::now() + 7d;

DEFINE INDEX IF NOT EXISTS idx_rag_cache_user ON rag_cache FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_rag_cache_hnsw ON rag_cache FIELDS query_embedding HNSW DIMENSION 768 DIST COSINE;

-- ── 4. kv_cache ───────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS kv_cache SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS cache_key       ON kv_cache TYPE string;
DEFINE FIELD IF NOT EXISTS cache_value     ON kv_cache TYPE string;
DEFINE FIELD IF NOT EXISTS expires_at      ON kv_cache TYPE option<datetime>;
DEFINE FIELD IF NOT EXISTS created_at      ON kv_cache TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_kv_cache_key ON kv_cache FIELDS cache_key UNIQUE;

-- ── 5. audit_logs ─────────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS audit_logs SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS action          ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS user_id         ON audit_logs TYPE string;
DEFINE FIELD IF NOT EXISTS doc_uuid        ON audit_logs TYPE option<string>;
DEFINE FIELD IF NOT EXISTS metadata        ON audit_logs TYPE string DEFAULT "{}";
DEFINE FIELD IF NOT EXISTS ip_address      ON audit_logs TYPE string DEFAULT "";
DEFINE FIELD IF NOT EXISTS timestamp       ON audit_logs TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_audit_user  ON audit_logs FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_audit_time  ON audit_logs FIELDS timestamp;

-- ── 6. user_memories ─────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS user_memories SCHEMAFULL;

DEFINE FIELD IF NOT EXISTS user_id          ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS memory_text      ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS category         ON user_memories TYPE string DEFAULT "general";
DEFINE FIELD IF NOT EXISTS confidence       ON user_memories TYPE float DEFAULT 1.0;
DEFINE FIELD IF NOT EXISTS embedding        ON user_memories TYPE array<float>;
DEFINE FIELD IF NOT EXISTS last_accessed_at ON user_memories TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS created_at       ON user_memories TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mem_user     ON user_memories FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_mem_cat      ON user_memories FIELDS category;
DEFINE INDEX IF NOT EXISTS idx_mem_hnsw     ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- ── 7. system_settings ───────────────────────────────────────
DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS monthly_budget_usd ON system_settings TYPE float DEFAULT 10.00;
DEFINE FIELD IF NOT EXISTS selected_model     ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS currency           ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS openrouter_api_key ON system_settings TYPE string DEFAULT "";

-- ── 8. context_cache ─────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS context_cache SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS context_hash    ON context_cache TYPE string;
DEFINE FIELD IF NOT EXISTS doc_uuid        ON context_cache TYPE option<string>;
DEFINE FIELD IF NOT EXISTS user_id         ON context_cache TYPE option<string>;
DEFINE FIELD IF NOT EXISTS context_text    ON context_cache TYPE string;
DEFINE FIELD IF NOT EXISTS token_count     ON context_cache TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS hit_count       ON context_cache TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS expires_at      ON context_cache TYPE datetime DEFAULT time::now() + 3d;
DEFINE FIELD IF NOT EXISTS created_at      ON context_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS updated_at      ON context_cache TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_context_hash ON context_cache FIELDS context_hash UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_context_doc  ON context_cache FIELDS doc_uuid;
DEFINE INDEX IF NOT EXISTS idx_context_exp  ON context_cache FIELDS expires_at;

-- ── 9. rate_limits ───────────────────────────────────────────
DEFINE TABLE IF NOT EXISTS rate_limits SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS key             ON rate_limits TYPE string;
DEFINE FIELD IF NOT EXISTS request_count   ON rate_limits TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS token_count     ON rate_limits TYPE int DEFAULT 0;
DEFINE FIELD IF NOT EXISTS window_start    ON rate_limits TYPE datetime DEFAULT time::now();
DEFINE FIELD IF NOT EXISTS expires_at      ON rate_limits TYPE datetime DEFAULT time::now() + 1h;

DEFINE INDEX IF NOT EXISTS idx_rate_limits_key ON rate_limits FIELDS key UNIQUE;
DEFINE INDEX IF NOT EXISTS idx_rate_limits_exp ON rate_limits FIELDS expires_at;
```

---

## 7. Deploying to Cloud Run

We deploy both the `web` service and the `worker` service. Deployments are managed declaratively using Knative service specifications in `service.yaml` and `service-worker.yaml` (which reference Secret Manager for credentials).

### Deploy Services Declaratively (Recommended)

Cloud Build computes the release version before it builds or deploys. The production
container, GitHub verification workflow, local checks, and SonarQube analysis use
the Python 3.14 runtime declared by project configuration. It passes that
same value to Cloud Run and SonarQube, so a production issue can be traced to one
release. The Kaniko build step uses an official, digest-pinned debug image because
it needs BusyBox to source computed release metadata; the standard executor image
does not include a shell. Its layer cache is stored in Artifact Registry and
network-sensitive operations use bounded retries:

```bash
# Run the pipeline to build and deploy web + worker services
RELEASE_VERSION=$(python scripts/update_docs.py --print-version)
DEPLOY_COMMIT_SHA=$(git rev-parse HEAD)
gcloud builds submit --config infra/gcp/cloudbuild.yaml \
  --substitutions="_RELEASE_VERSION=${RELEASE_VERSION},_DEPLOY_COMMIT_SHA=${DEPLOY_COMMIT_SHA}"
```

### Emergency recovery

Do not deploy individual services with ad-hoc `gcloud run deploy` commands.
They drift secret bindings, service configuration, and release metadata. Recover
through the reviewed Cloud Build configuration; Pulumi will become the supported
provisioning and reconciliation path after infrastructure import and preview.

---

## 8. Continuous Updates & Redeployment

Whenever you update your code, run the local verification suite first. Its differential pre-commit gate runs Bandit, Semgrep, AST-Grep, and ShellCheck on relevant changed files and rejects newly added unreasoned suppressions. Pipeline failures propagate through output capture, so a failed test cannot be reported as successful. With SonarQube Community Edition, the remote PR pipeline blocks on repository-native shift-left checks and GitHub security tools, then publishes a read-only table of the current `main` quality-gate baseline; the authoritative SonarQube quality-gate log, summary, annotations, and dashboard link apply to `main` after a push. Cloud Build automatically builds the immutable container, but updates both Cloud Run services only after the exact commit's mainline gate succeeds:

CI installs Python security scanners in an isolated environment if their dependency graph differs from the application runtime. AST-Grep is invoked through its pinned official npm CLI because the similarly named Python package does not expose a command-line executable. This preserves reproducible application tests and keeps all scanner results blocking.

```bash
RELEASE_VERSION=$(python scripts/update_docs.py --print-version)
DEPLOY_COMMIT_SHA=$(git rev-parse HEAD)
gcloud builds submit --config infra/gcp/cloudbuild.yaml \
  --substitutions="_RELEASE_VERSION=${RELEASE_VERSION},_DEPLOY_COMMIT_SHA=${DEPLOY_COMMIT_SHA}"
```

## Protected PR branch refresh

The GitHub workflow `refresh-pr-branches.yml` updates eligible same-repository
PR branches after `main` advances so protected checks run against the current
base. It uses the dedicated `PR_AUTOMATION_TOKEN` repository secret and never
performs deployments or merges pull requests.

---

## 9. MCP Triage & Diagnostic Observability

AI agents and platform operators leverage Model Context Protocol (MCP) servers for live, zero-overhead diagnostic triage across the deployment lifecycle:

- **Google Cloud Logging MCP (`google-cloud-logging`)**: Triage Cloud Run service logs (`get_service_log`, `list_log_entries`) to pinpoint unhandled container exceptions or startup timeouts without navigating the GCP web console.
- **Google Cloud Monitoring MCP (`google-cloud-monitoring`)**: Query live Cloud Run latency percentiles, worker CPU/memory usage, and Cloud Tasks queue depths (`list_timeseries`, `list_alerts`).
- **SonarQube MCP (`sonarqube`)**: Query real-time quality gate status, rule violations, and security hotspots (`get_project_quality_gate_status`, `search_sonar_issues_in_projects`).
- **Chrome DevTools MCP (`chrome-devtools-mcp`)**: Audit production frontend performance, Core Web Vitals, accessibility compliance (`a11y-debugging`), and browser runtime console errors.
- **Google Developer Knowledge MCP (`google-developer-knowledge`)**: Verify up-to-date Cloud Run and Vertex AI configuration blueprints.
