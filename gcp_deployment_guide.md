# Google Cloud Run Production Deployment Guide (Version 2.0)

This guide describes how to provision, configure, build, and deploy the **AetherOmni** application to production on **Google Cloud Run**, utilizing a SQLite metadata database, **SurrealDB** for vector storage/RAG caches, **Google Cloud Tasks** for background task queuing, Google Cloud Storage, and Google Secret Manager.

---

## 1. Production Architecture Overview

The production system consists of:
1. **Cloud Run Service (`web`)**: Handles user HTTP traffic, serves pages, static files, and houses SQLite databases. Also acts as the webhook target for asynchronous task executions.
2. **Google Cloud Tasks Queue (`extractor-tasks`)**: Orchestrates background document processing. Instead of a persistent VM or always-on worker service, tasks are dispatched to Cloud Tasks and executed via secure, OIDC-authorized HTTP POST requests targeting the `/internal/tasks/<task_name>/` endpoint on the `web` container.
3. **SurrealDB (REST/WebSockets)**: Standard SurrealDB instance deployed on GCE or Cloud Run (with persistent disk or network storage), exposing port 8000. Houses:
   - `chunks`: Stores document text chunks and their 768-dimension `text-embedding-004` vectors with an HNSW index.
   - `rag_cache`: Semantic cache for grounded answers.
   - `kv_cache`: Fast key-value exact-match cache.
   - `user_memories`: Long-term user preferences vectors.
4. **Cloud Storage (GCS) or Supabase Storage**: Private storage for holding document uploads.
5. **Secret Manager**: Securely stores environment credentials (`DJANGO_SECRET_KEY`, `SURREAL_URL`, `SURREAL_USER`, `SURREAL_PASS`, `GEMINI_API_KEY`, `SUPABASE_REALTIME_URL`, `SUPABASE_REALTIME_KEY`).

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
echo -n "http://YOUR_SURREALDB_HOST:8000" | gcloud secrets versions add SURREAL_URL --data-file=-

gcloud secrets create SURREAL_USER --replication-policy="automatic"
echo -n "admin" | gcloud secrets versions add SURREAL_USER --data-file=-

gcloud secrets create SURREAL_PASS --replication-policy="automatic"
echo -n "YOUR_SURREALDB_PASSWORD" | gcloud secrets versions add SURREAL_PASS --data-file=-
```

### B. Grant Secret Access to the Service Account
```bash
for secret in DJANGO_SECRET_KEY GEMINI_API_KEY SURREAL_URL SURREAL_USER SURREAL_PASS; do
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
-- Define the chunks table (exposing HNSW 768-dimension vectors)
DEFINE TABLE chunks SCHEMAFULL;
DEFINE FIELD doc_uuid ON TABLE chunks TYPE string;
DEFINE FIELD chunk_index ON TABLE chunks TYPE int;
DEFINE FIELD content ON TABLE chunks TYPE string;
DEFINE FIELD embedding ON TABLE chunks TYPE array<float>;
DEFINE FIELD created_at ON TABLE chunks TYPE datetime DEFAULT time::now();
DEFINE INDEX idx_chunks_doc ON chunks FIELDS doc_uuid;
DEFINE INDEX idx_chunks_hnsw ON chunks FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- Define KV cache table
DEFINE TABLE kv_cache SCHEMAFULL;
DEFINE FIELD cache_key ON TABLE kv_cache TYPE string;
DEFINE FIELD cache_value ON TABLE kv_cache TYPE string;
DEFINE FIELD expires_at ON TABLE kv_cache TYPE option<datetime>;
DEFINE INDEX idx_kv_cache_key ON kv_cache FIELDS cache_key UNIQUE;

-- Define RAG search cache table (HNSW index for fuzzy matching cached queries)
DEFINE TABLE rag_cache SCHEMAFULL;
DEFINE FIELD query_text ON TABLE rag_cache TYPE string;
DEFINE FIELD query_embedding ON TABLE rag_cache TYPE array<float>;
DEFINE FIELD answer_text ON TABLE rag_cache TYPE string;
DEFINE FIELD sources ON TABLE rag_cache TYPE array<string>;
DEFINE FIELD user_id ON TABLE rag_cache TYPE string;
DEFINE FIELD created_at ON TABLE rag_cache TYPE datetime DEFAULT time::now();
DEFINE FIELD expires_at ON TABLE rag_cache TYPE datetime DEFAULT time::now() + 7d;
DEFINE INDEX idx_rag_cache_user ON rag_cache FIELDS user_id;
DEFINE INDEX idx_rag_cache_hnsw ON rag_cache FIELDS query_embedding HNSW DIMENSION 768 DIST COSINE;

-- Define User memories table (HNSW index for user preference alignment)
DEFINE TABLE user_memories SCHEMAFULL;
DEFINE FIELD user_id ON TABLE user_memories TYPE string;
DEFINE FIELD memory_text ON TABLE user_memories TYPE string;
DEFINE FIELD embedding ON TABLE user_memories TYPE array<float>;
DEFINE FIELD created_at ON TABLE user_memories TYPE datetime DEFAULT time::now();
DEFINE INDEX idx_mem_user ON user_memories FIELDS user_id;
DEFINE INDEX idx_mem_hnsw ON user_memories FIELDS embedding HNSW DIMENSION 768 DIST COSINE;

-- Define Compliance Audit Logs table
DEFINE TABLE audit_logs SCHEMAFULL;
DEFINE FIELD action ON audit_logs TYPE string;
DEFINE FIELD user_id ON audit_logs TYPE string;
DEFINE FIELD doc_uuid ON audit_logs TYPE option<string>;
DEFINE FIELD metadata ON audit_logs TYPE string DEFAULT "{}";
DEFINE FIELD ip_address ON audit_logs TYPE string DEFAULT "";
DEFINE FIELD timestamp ON audit_logs TYPE datetime DEFAULT time::now();
DEFINE INDEX idx_audit_user ON audit_logs FIELDS user_id;
DEFINE INDEX idx_audit_time ON audit_logs FIELDS timestamp;
```

---

## 7. Deploying to Cloud Run

We deploy the single `web` service. Django runs SQLite locally on the instance disk (with optional volume mounts or persistent configurations for zero-loss scaling).

### Deploy Web Service
```bash
gcloud run deploy data-extractor-web \
  --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/web-app:latest \
  --region=${REGION} \
  --service-account=${SERVICE_ACCOUNT} \
  --set-env-vars="DJANGO_DEBUG=False,GS_BUCKET_NAME=${BUCKET_NAME},DJANGO_ALLOWED_HOSTS=*.run.app,DJANGO_CSRF_TRUSTED_ORIGINS=https://*.run.app,GCP_PROJECT_ID=${PROJECT_ID},GCP_QUEUE_LOCATION=${REGION},GCP_QUEUE_NAME=${QUEUE_NAME}" \
  --set-secrets="DJANGO_SECRET_KEY=DJANGO_SECRET_KEY:latest,GEMINI_API_KEY=GEMINI_API_KEY:latest,SURREAL_URL=SURREAL_URL:latest,SURREAL_USER=SURREAL_USER:latest,SURREAL_PASS=SURREAL_PASS:latest" \
  --allow-unauthenticated
```

---

## 8. Continuous Updates & Redeployment

Whenever you update your code:

1. **Re-build & Push Image**:
   ```bash
   gcloud builds submit --tag ${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/web-app:latest
   ```
2. **Update Service**:
   ```bash
   gcloud run services update data-extractor-web \
     --region=${REGION} \
     --image=${REGION}-docker.pkg.dev/${PROJECT_ID}/${ARTIFACT_REGISTRY}/web-app:latest
   ```
