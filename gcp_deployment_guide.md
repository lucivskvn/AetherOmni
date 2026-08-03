# Google Cloud Run Production Deployment Guide (Version 1.2.286)

This guide describes how to provision, configure, build, and deploy the **AetherOmni** application to production on **Google Cloud Run**, utilizing a SQLite metadata database, **SurrealDB** for vector storage/RAG caches, **Google Cloud Tasks** for background task queuing, Google Cloud Storage, and Google Secret Manager.

---

## 1. Production Architecture Overview

The production system consists of:
1. **Cloud Run Service (`aether-web`)**: Handles user HTTP traffic, serves dashboard/login pages, and houses ephemeral SQLite databases for Django's user sessions.
2. **Cloud Run Service (`aether-worker`)**: Dedicated worker instance that processes heavy background OCR and RAG ingestion.
3. **Google Cloud Tasks Queue (`extractor-tasks`)**: Orchestrates background document processing. Tasks are dispatched from `web` to Cloud Tasks, which trigger HTTP POST callbacks targeting the `/internal/tasks/<task_name>/` endpoint on the `worker` service.
4. **Remote SurrealDB (rpc via WebSockets)**: Deployed as a secure, standalone service (at `wss://surrealdb.fainko.cloud/rpc`). It serves as the primary database store for all document metadata (`SourceDocument`), compliance audit logs (`AuditLog`), system settings (`SystemSettings`), vector chunk databases (`chunks`), and semantic search caches (`rag_cache`).
5. **Cloud Storage (GCS)**: Stores raw, uploaded PDF assets securely.
6. **Supabase Auth (GoTrue REST API)**: Handles user credentials, login, and registration securely on a self-hosted instance (at `https://supabase.fainko.cloud`).
7. **Secret Manager**: Securely stores environment credentials (`DJANGO_SECRET_KEY`, `SURREAL_URL`, `SURREAL_USER`, `SURREAL_PASS`, `GEMINI_API_KEY`, `SUPABASE_URL`, `SUPABASE_PUBLIC_KEY`).

---

## 2. Resource Provisioning

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

## 3. Security & IAM Configuration

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

# 4. Administrative Credentials (for automatic dashboard and local auth stub creation)
gcloud secrets create ADMIN_EMAIL --replication-policy="automatic"
echo -n "admin@example.com" | gcloud secrets versions add ADMIN_EMAIL --data-file=-

gcloud secrets create ADMIN_USERNAME --replication-policy="automatic"
echo -n "admin" | gcloud secrets versions add ADMIN_USERNAME --data-file=-

gcloud secrets create ADMIN_PASSWORD --replication-policy="automatic"
echo -n "AdminPass123!" | gcloud secrets versions add ADMIN_PASSWORD --data-file=-

# 5. Supabase Auth Configuration (for user authentication)
gcloud secrets create SUPABASE_URL --replication-policy="automatic"
echo -n "https://supabase.fainko.cloud" | gcloud secrets versions add SUPABASE_URL --data-file=-

gcloud secrets create SUPABASE_PUBLIC_KEY --replication-policy="automatic"
echo -n "YOUR_SUPABASE_PUBLIC_KEY" | gcloud secrets versions add SUPABASE_PUBLIC_KEY --data-file=-
```

### B. Grant Secret Access to the Service Account
```bash
for secret in DJANGO_SECRET_KEY GEMINI_API_KEY SURREAL_URL SURREAL_USER SURREAL_PASS ADMIN_EMAIL ADMIN_USERNAME ADMIN_PASSWORD SUPABASE_URL SUPABASE_PUBLIC_KEY; do
  gcloud secrets add-iam-policy-binding ${secret} \
    --member="serviceAccount:${SERVICE_ACCOUNT}" \
    --role="roles/secretmanager.secretAccessor"
done
```

---

## 5. Build Container Images

Use Google Cloud Build to build and push your Docker container:
```bash
gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/web-app:latest
```

---

## 6. SurrealDB Setup and HNSW Vector Indexes

### Automated Schema Bootstrap
The database schema is automatically bootstrapped and verified on container boot by `init_surreal.py` (which runs automatically as the Docker `web` container entrypoint). It waits for SurrealDB to become healthy, ensures the namespace and database are pre-defined, and imports the full schema.

### Manual Schema Initialization
If you need to manually initialize or verify the SurrealDB schema, run the following queries (fully compatible with SurrealDB 3.x HNSW syntax):

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

DEFINE FIELD IF NOT EXISTS user_id         ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS memory_text     ON user_memories TYPE string;
DEFINE FIELD IF NOT EXISTS embedding       ON user_memories TYPE array<float>;
DEFINE FIELD IF NOT EXISTS created_at      ON user_memories TYPE datetime DEFAULT time::now();

DEFINE INDEX IF NOT EXISTS idx_mem_user    ON user_memories FIELDS user_id;
DEFINE INDEX IF NOT EXISTS idx_mem_hnsw    ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- ── 7. system_settings ───────────────────────────────────────
DEFINE TABLE IF NOT EXISTS system_settings SCHEMAFULL;
DEFINE FIELD IF NOT EXISTS monthly_budget_usd ON system_settings TYPE float DEFAULT 10.00;
DEFINE FIELD IF NOT EXISTS selected_model     ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS currency           ON system_settings TYPE string DEFAULT "auto";
DEFINE FIELD IF NOT EXISTS openrouter_api_key ON system_settings TYPE string DEFAULT "";
```

---

## 7. Deploying to Cloud Run

We deploy both the `web` service and the `worker` service. Deployments are managed declaratively using Knative service specifications in `service.yaml` and `service-worker.yaml` (which reference Secret Manager for credentials).

### Deploy Services Declaratively (Recommended)
Cloud Build handles compiling the image, tagging it with the current `$BUILD_ID`, replacing the image references in the Knative YAMLs, and applying them:
```bash
# Run the pipeline to build and deploy web + worker services
gcloud builds submit --config cloudbuild.yaml
```

### Deploying Services Manually
If you want to deploy manually using the CLI:
```bash
# Deploy Web Service
gcloud run deploy aether-web \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/aether-web:latest \
  --region=${REGION} \
  --service-account=${SERVICE_ACCOUNT} \
  --set-env-vars="DJANGO_DEBUG=False,GS_BUCKET_NAME=${BUCKET_NAME},DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.run.app,DJANGO_CSRF_TRUSTED_ORIGINS=https://*.run.app,GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},GCP_QUEUE_LOCATION=${REGION},GCP_QUEUE_NAME=${QUEUE_NAME},APP_URL=https://aether-web-751922320980.asia-southeast1.run.app,WORKER_URL=https://aether-worker-751922320980.asia-southeast1.run.app,SURREAL_NS=aetheromni,SURREAL_DB=extractor,SURREALDB_OFFLINE=False" \
  --set-secrets="DJANGO_SECRET_KEY=DJANGO_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SURREAL_URL=SURREAL_URL:latest,SURREAL_USER=SURREAL_USER:latest,SURREAL_PASS=SURREAL_PASS:latest,ADMIN_EMAIL=ADMIN_EMAIL:latest,ADMIN_USERNAME=ADMIN_USERNAME:latest,ADMIN_PASSWORD=ADMIN_PASSWORD:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_PUBLIC_KEY=SUPABASE_PUBLIC_KEY:latest" \
  --allow-unauthenticated

# Deploy Worker Service
gcloud run deploy aether-worker \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/aether-web:latest \
  --region=${REGION} \
  --service-account=${SERVICE_ACCOUNT} \
  --set-env-vars="DJANGO_DEBUG=False,GS_BUCKET_NAME=${BUCKET_NAME},DJANGO_ALLOWED_HOSTS=localhost,127.0.0.1,.run.app,DJANGO_CSRF_TRUSTED_ORIGINS=https://*.run.app,GCP_PROJECT_ID=${PROJECT_ID},GCP_REGION=${REGION},GCP_QUEUE_LOCATION=${REGION},GCP_QUEUE_NAME=${QUEUE_NAME},APP_URL=https://aether-worker-751922320980.asia-southeast1.run.app,SURREAL_NS=aetheromni,SURREAL_DB=extractor,SURREALDB_OFFLINE=False" \
  --set-secrets="DJANGO_SECRET_KEY=DJANGO_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SURREAL_URL=SURREAL_URL:latest,SURREAL_USER=SURREAL_USER:latest,SURREAL_PASS=SURREAL_PASS:latest,ADMIN_EMAIL=ADMIN_EMAIL:latest,ADMIN_USERNAME=ADMIN_USERNAME:latest,ADMIN_PASSWORD=ADMIN_PASSWORD:latest,SUPABASE_URL=SUPABASE_URL:latest,SUPABASE_PUBLIC_KEY=SUPABASE_PUBLIC_KEY:latest" \
  --allow-unauthenticated
```

---

## 8. Continuous Updates & Redeployment

Whenever you update your code, run the Cloud Build pipeline. This automatically builds the container, registers it in the Google Artifact Registry, and performs a zero-downtime rolling update of both Cloud Run services:

```bash
gcloud builds submit --config cloudbuild.yaml
```
