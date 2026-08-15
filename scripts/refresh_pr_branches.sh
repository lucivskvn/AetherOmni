#!/usr/bin/env bash
# ==============================================================================
# scripts/refresh_pr_branches.sh
# Refreshes open same-repository PR branches targeting 'main'.
#
# Usage:
#   bash scripts/refresh_pr_branches.sh              # Refreshes all open eligible PRs
#   bash scripts/refresh_pr_branches.sh <PR_NUMBER>  # Refreshes a specific PR
#   bash scripts/refresh_pr_branches.sh --workflow   # Dispatches GitHub Actions workflow
# ==============================================================================
set -euo pipefail

TARGET_PR="${1:-}"

if ! command -v gh &> /dev/null; then
    echo "Error: GitHub CLI ('gh') is required. Install: https://cli.github.com" >&2
    exit 1
fi

if [ "$TARGET_PR" = "--workflow" ]; then
    echo "Triggering remote GitHub Actions workflow (refresh-pr-branches.yml)..."
    gh workflow run refresh-pr-branches.yml
    echo "✓ Workflow dispatch triggered."
    exit 0
fi

if [ -n "$TARGET_PR" ] && [[ "$TARGET_PR" =~ ^[0-9]+$ ]]; then
    echo "Updating branch for PR #${TARGET_PR} against origin/main..."
    gh pr update-branch "$TARGET_PR"
    echo "✓ PR #${TARGET_PR} updated successfully."
    exit 0
fi

echo "Querying open, non-draft PRs targeting 'main'..."
OPEN_PRS=$(gh pr list --base main --state open --json number,headRefName,isDraft --jq '.[] | select(.isDraft == false) | .number' || true)

if [ -z "$OPEN_PRS" ]; then
    echo "No open non-draft PRs targeting 'main' found."
    exit 0
fi

for pr in $OPEN_PRS; do
    echo "Updating branch for PR #${pr}..."
    gh pr update-branch "$pr" || echo "Warning: Failed to update PR #${pr}, continuing..."
done

echo "✓ All eligible PR branches refreshed."
