#!/bin/bash
# ==============================================================================
# GCP CLOUD DIAGNOSTICS & LIVE LOG TAILING TOOL FOR AI AGENTS & CLI
# ==============================================================================
SERVICE_NAME="${1:-aetheromni}"
REGION="${2:-asia-southeast1}"
# Project ID — override via 3rd positional arg, GCP_PROJECT env var, or gcloud config
DEFAULT_PROJECT=$(gcloud config get-value project 2>/dev/null || echo "your-gcp-project-id")
PROJECT_ID="${3:-${GCP_PROJECT:-$DEFAULT_PROJECT}}"

echo "======================================================================"
echo "🔍 GCP CLOUD DIAGNOSTICS & RUNTIME METRICS AUDIT"
echo "   Service: $SERVICE_NAME | Region: $REGION | Project: $PROJECT_ID"
echo "   Timestamp: $(date)"
echo "======================================================================"

echo ""
echo "📌 [1/3] Fetching Cloud Run Service Configuration & Status..."
gcloud run services describe "$SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" --format="yaml(status,spec.template.spec.containers)" 2>/dev/null || echo "   ⚠️ Service not found or gcloud unauthenticated."

echo ""
echo "🚨 [2/3] Fetching Recent Runtime Errors & Stack Traces (Last 1 Hour)..."
gcloud logging read "resource.type=cloud_run_revision AND resource.labels.service_name=$SERVICE_NAME AND severity>=ERROR" --project="$PROJECT_ID" --limit=10 --format="table(timestamp,severity,textPayload,jsonPayload.message)" 2>/dev/null || echo "   ℹ️ No runtime errors detected."

echo ""
echo "📊 [3/3] Recent Deployment Revisions & Health..."
gcloud run revisions list --service="$SERVICE_NAME" --project="$PROJECT_ID" --region="$REGION" --limit=3 2>/dev/null || true

echo "======================================================================"
