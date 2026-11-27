# KORDA — Log Triage Runbook

Field guide for the most common production symptoms. All commands assume
`gcloud` authenticated against the KORDA project (`esbpcs-lab-un`) and the
Pulumi-managed services `korda-web` / `korda-worker`.

## Quick orientation

| Surface | Where to look |
| --- | --- |
| HTTP 4xx/5xx, auth failures | `korda-web` / `korda-worker` request logs |
| Ingestion pipeline | `korda-worker` app logs (`doc_id`, `step`, `status`, `latency_ms`) |
| LLM cost/quotas | `document_spend_reconciliations`, `monthly_spend_ledger` (SurrealDB) |
| Queue backlog | Cloud Tasks queue `extractor-tasks-v2` metrics |
| Cold-start latency | Cloud Run instance count + startup probe logs |

Structured app logs carry `trace_id`, `doc_id`, `media_type`, `step`,
`status`, `latency_ms` — filter on `jsonPayload.doc_id` to follow one document.

## 401 / 403 — authentication & authorization

**Symptoms:** `The request was not authenticated` (Cloud Run platform log),
`{"error": "Default password change required."}`, OIDC verification failures on
`/internal/tasks/` or `/internal/events/gcs-raw/`.

- Worker is intentionally private (`--no-allow-unauthenticated`). Unauthenticated
  hits are internet scanners — expected noise, safe to ignore. Verify volume:

  ```bash
  gcloud logging read 'resource.labels.service_name="korda-worker" AND severity>=WARNING' \
    --project=esbpcs-lab-un --freshness=1h --limit=20
  ```

- Cloud Tasks/Eventarc deliveries failing OIDC: check the invoker service
  account bindings in Pulumi (`worker_invoker_binding`, Eventarc trigger SA)
  and that `CLOUD_TASKS_SERVICE_ACCOUNT`/`EVENTARC_SERVICE_ACCOUNT` env vars
  match deployed values.
- 403 `Default password change required`: the seeded admin still uses `admin`;
  sign in and complete the forced password change (see
  `core/middleware.py::ForcePasswordChangeMiddleware`).
- Supabase auth failures: verify `SUPABASE_URL`/`SUPABASE_PUBLIC_KEY` secrets
  and that Turnstile site/secret keys are paired (`CF_TURNSTILE_SITE_KEY`).

## 429 / 503 / 504 — quota, rate limits, timeouts

- **App rate limiter (429):** distributed rate-limit backend in SurrealDB
  (`rate_limits` table). Contract: backend failure **denies** the request —
  a SurrealDB outage surfaces as 429/5xx, not silent pass. Check SurrealDB
  health first:

  ```bash
  gcloud logging read 'resource.labels.service_name="korda-worker" AND jsonPayload.step:"surreal"' \
    --project=esbpcs-lab-un --freshness=30m
  ```

- **LLM provider 429/503:** Vertex AI / Gemini quota or transient outage.
  `extractor/llm_gateway.py` retries with backoff; persistent failures mark
  the document `FAILED` with `error_message`. Check spend ledger before
  assuming quota — `MONTHLY_BUDGET_USD` cap also produces budget-exceeded
  failures by design.
- **504 / task timeout:** Cloud Tasks dispatch deadline is 1740s
  (`extractor/cloud_tasks.py`) against a Cloud Run worker `timeout=1800s`.
  Pipelines past the ~25-minute heartbeat budget are treated as dead by the
  stale-task reaper — check `latency_ms` on the failing step and queue retry
  policy (`infra/pulumi/__main__.py` `QueueRetryConfigArgs`).
- **Cloud Run 429 (concurrency):** `max_instance_count` bound hit during
  ingest bursts; scale-up is bounded by design — retry is automatic via
  Cloud Tasks.

## Media extraction failures

Symptoms: document stuck `EXTRACTING`/`REFINING`, `error_message` populated,
Eventarc trigger not firing.

Checklist:

1. Eventarc trigger exists and matches `objects/raw/**` on the media bucket
   (`infra/pulumi/components.py::EventarcRawTrigger`).
2. Upload landed under `raw/` — objects outside `raw/` never trigger
   ingestion (`task_handlers.py` rejects non-`raw/` names and `..` traversal).
3. Non-empty guards: empty downloads/transcripts abort before embedding —
   check file integrity in GCS and parser-specific budgets (XLSX archive /
   row / cell limits).
4. `gcs_status="purged"` sources render as **tombstone** cards in grading —
   the quote survives, the media link does not. This is intended behavior
   after the 1-day `raw/ephemeral/` lifecycle TTL, not data loss of text.

## GCS lifecycle surprises

- `raw/ephemeral/` objects die after **1 day** — re-ingestion or late review
  of raw uploads will 404. Derived data (chunks, anchors, crystals) persists
  in SurrealDB; only the physical media is ephemeral.
- `exports/` objects die after 14 days; regenerate exports rather than
  expecting old links to work.
- Backup bucket keeps 7-day soft-delete + versioning — recovery window for
  accidental overwrites during namespace migration
  (`scripts/migrate_surreal_namespace.py`).

## SurrealQL issues

- **Schema drift:** `schema.surql` is applied at startup
  (`RUN_MIGRATIONS_ON_STARTUP`) and validated in CI (`surreal validate`).
  New tables must also be added to `TABLES_TO_MIGRATE` in
  `scripts/migrate_surreal_namespace.py` — `check_reliability_contracts.py`
  enforces set-equality with `DEFINE TABLE` statements.
- **Transport errors:** production refuses the `ws` scheme — confirm `SURREAL_URL`
  uses `wss://` and ends with `/rpc` (auto-appended if missing).
- **Namespace auto-detection:** if `SURREAL_NS` is unset the client probes
  namespaces; always set it explicitly (Pulumi does).
- **Record-id injection:** record ids in raw SurrealQL are validated against
  a strict pattern — suspicious ids raise `ValueError` rather than executing.

## Escalation

- Sentry project `fainko` captures unhandled exceptions with release context.
- `scripts/gcp-diagnostics.sh <service>` gives read-only Cloud Run health.
- Never paste secrets into tickets; Secret Manager values are masked on read.
