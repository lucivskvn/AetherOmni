#!/bin/bash
# Read-only Cloud Run health and error diagnostic tool.
# Usage: bash scripts/gcp-diagnostics.sh [--service aether-web|aether-worker|all]
#        [--project PROJECT_ID] [--region REGION] [--since DURATION]

set -euo pipefail

PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT:-}}}"
REGION="${GCP_REGION:-asia-southeast1}"
SERVICE="aether-web"
SINCE="1h"

usage() {
    echo "Usage: $0 [--service aether-web|aether-worker|all] [--project PROJECT_ID] [--region REGION] [--since DURATION]" >&2
}

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --service) SERVICE="$2"; shift 2 ;;
        --project) PROJECT_ID="$2"; shift 2 ;;
        --region) REGION="$2"; shift 2 ;;
        --since) SINCE="$2"; shift 2 ;;
        --help|-h) usage; exit 0 ;;
        *) usage; exit 2 ;;
    esac
done

if ! command -v gcloud >/dev/null 2>&1; then
    echo "ERROR: gcloud CLI is required." >&2
    exit 1
fi

if [[ -z "$PROJECT_ID" ]]; then
    PROJECT_ID=$(gcloud config get-value project 2>/dev/null || true)
fi
if [[ -z "$PROJECT_ID" ]] || [[ "$PROJECT_ID" = "(unset)" ]]; then
    echo "ERROR: set --project, GCP_PROJECT_ID, or a gcloud default project." >&2
    exit 1
fi

case "$SERVICE" in
    aether-web|aether-worker) SERVICES=("$SERVICE") ;;
    all) SERVICES=(aether-web aether-worker) ;;
    *) echo "ERROR: unsupported service: $SERVICE" >&2; exit 2 ;;
esac

echo "GCP Cloud Run diagnostics"
echo "Project: $PROJECT_ID | Region: $REGION | Errors since: $SINCE"

for service_name in "${SERVICES[@]}"; do
    echo
    echo "== $service_name =="
    if ! gcloud run services describe "$service_name" --project="$PROJECT_ID" --region="$REGION" \
        --format='table(status.url:label=URL,status.latestReadyRevisionName:label=READY_REVISION,status.latestCreatedRevisionName:label=LATEST_REVISION)' ; then
        echo "Service is unavailable or cannot be described." >&2
        continue
    fi

    echo "Recent revisions:"
    gcloud run revisions list --service="$service_name" --project="$PROJECT_ID" --region="$REGION" \
        --limit=3 --format='table(metadata.name,status.conditions[0].status:label=READY,metadata.creationTimestamp:label=CREATED)' || true

    echo "Recent errors:"
    gcloud logging read \
        "resource.type=cloud_run_revision AND resource.labels.service_name=$service_name AND severity>=ERROR" \
        --project="$PROJECT_ID" --freshness="$SINCE" --limit=20 \
        --format='table(timestamp,severity,textPayload,jsonPayload.message)' || true
done
