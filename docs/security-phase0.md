# KORDA — Phase 0 Security & Boundary Artifacts

This document records the trust boundaries enforced by the KORDA stack and the
static checks that keep them from drifting. It describes the **implemented**
controls; `scripts/check_security_boundaries.py` fails CI/local gates when the
code or manifests stop matching this document.

> Spec mapping note: the reference design called for `infra/index.ts`
> (TypeScript Pulumi) and a `src/` service layout. KORDA's canonical
> implementation is **Python Pulumi** (`infra/pulumi/`) driving a **Django**
> application (`extractor/` + `core/`). The boundaries below are identical;
> only the file layout differs.

## 1. IAM least-privilege boundaries

Runtime identity: dedicated service account `korda-runner-sa` (created in
`infra/pulumi/__main__.py`), used by both Cloud Run services and Cloud Tasks.

| Grant | Scope | Role |
| --- | --- | --- |
| Project | runtime SA | `roles/aiplatform.user` (Vertex AI extract/embed) |
| Project | runtime SA | `roles/cloudtasks.enqueuer` |
| Project | runtime SA | `roles/run.viewer` (deployment controller reads worker only) |
| Project | runtime SA | `roles/eventarc.eventReceiver` |
| Media bucket only | runtime SA | `roles/storage.objectAdmin` |
| Backup bucket only | runtime SA | `roles/storage.objectCreator` (write-only, no read/delete) |
| Each secret individually | runtime SA | `roles/secretmanager.secretAccessor` — **no project-wide grant** |
| `korda-worker` service only | task-invoker SA | `roles/run.invoker` |
| `korda-web` service | runtime SA | `roles/run.invoker` |
| Application queue | task-invoker SA | `roles/cloudtasks.taskDeleter` (cancellation path) |

The deployment controller may scale only `WORKER_SERVICE_NAME`; superuser-only,
never substitutes the web service (enforced in `extractor/deployment.py` and
contract-checked).

## 2. GCS bucket posture

Both buckets are created by `ComplianceBucket` (`infra/pulumi/components.py`)
with `uniform_bucket_level_access=True` — no ACLs, no per-object IAM.

- **Media bucket** (`ephemeral=True`): Soft Delete retention `0`, versioning
  **off**, lifecycle deletes `raw/ephemeral/` objects after 1 day, `exports/`
  after `export_retention_days` (14), and aborts incomplete multipart uploads.
- **Backup bucket** (`ephemeral=False`): Soft Delete retention 7 days,
  versioning **on** — survives accidental overwrite during namespace rebuilds.

Spec deviation: lifecycle tiers are `raw/ephemeral/` + `exports/` (media) and a
separate versioned backup bucket, rather than `latest-only/`/`archive/`
prefixes. Same intent — hot data expires, cold data is versioned.

## 3. SurrealDB network & auth

- Transport: production requires `wss://`; `extractor/surreal_db.py`
  `_get_ws_url` raises when `DEBUG=False` and the URL is `ws://`/`http://`.
  Plaintext is permitted only in debug/offline (`SURREALDB_OFFLINE=True`,
  `TESTING`).
- Credentials: `SURREAL_USER`/`SURREAL_PASS` are mandatory outside debug; empty
  credentials raise `ImproperlyConfigured`. `scripts/init_surreal.py` aborts in
  production when the password is empty or `root`.
- Scope: namespace `korda`, database `extractor` (set by Pulumi env
  `SURREAL_NS`/`SURREAL_DB`); identifiers are validated against an allowlist
  pattern before schema import.
- Tenant isolation is enforced in application queries (`owner_id` = Supabase
  auth subject UUID on every read/write path).

## 4. Pulumi ESC & secret injection

`infra/pulumi/esc/*.yaml` holds environment configuration; secrets resolve via
Pulumi ESC / Secret Manager references — never literals in `Pulumi.*.yaml`.
Cloud Build verifies every required secret (including `SUPABASE_DATABASE_URL`)
exists before building, using the same project-derived names as Pulumi.

## 5. Cloud Run auth posture

- `korda-web`: public — `allUsers` `run.invoker` (browser access).
- `korda-worker`: **private** — deployed `--no-allow-unauthenticated`; invoked
  only via Cloud Tasks OIDC and Eventarc OIDC to `/internal/tasks/` and
  `/internal/events/gcs-raw/` (both CSRF-exempt but OIDC/token verified in
  `extractor/task_handlers.py`).
- Both scale to zero (`min_instance_count=0`); the worker's memory floor
  (2Gi / pinned `GUNICORN_WORKERS`) is contract-checked in Pulumi, manifests,
  and Cloud Build flags.

## 6. Application-layer boundaries

- Supabase Auth subject UUID is the tenant key for documents, RAG, exports,
  rate limits; Django integer IDs are offline-only.
- `ADMIN_EMAIL` is the only administrative authority; Supabase app metadata
  never grants Django superuser.
- Credential inputs are never rendered for deployment-managed secrets.
- Rate-limit backends fail closed; dedup is tenant-scoped.
- SurrealDB is the sole deletion-spend accounting authority in production;
  the Django ledger signal is offline-only (no double counting).

## 7. Verification

```bash
python3 scripts/check_security_boundaries.py   # static boundary assertions
bash run_checks.sh                              # canonical full gate
```

The checker asserts the invariants above directly against
`infra/pulumi/components.py`, `infra/pulumi/__main__.py`,
`infra/gcp/service*.yaml`, `infra/gcp/cloudbuild.yaml`,
`extractor/surreal_db.py`, and `core/settings.py`.
