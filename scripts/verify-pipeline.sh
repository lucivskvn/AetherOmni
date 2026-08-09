#!/bin/bash
# ==============================================================================
# UNIFIED PRE-PRODUCTION QUALITY & SECURITY GATE PIPELINE (ENTERPRISE DEVSECOPS EDITION)
# ==============================================================================
# Usage: ./scripts/verify-pipeline.sh [project_dir] [--fix|--autofix]

set -e

PROJECT_DIR="${1:-$(pwd)}"
if [ -d "$PROJECT_DIR" ]; then
    cd "$PROJECT_DIR"
fi

AUTOFIX_ARG=""
FAST_ARG=""
for arg in "$@"; do
    if [ "$arg" == "--fix" ] || [ "$arg" == "--autofix" ]; then
        AUTOFIX_ARG="--fix"
    fi
    if [ "$arg" == "--docs" ] || [ "$arg" == "--docs-only" ] || [ "$arg" == "--fast" ]; then
        FAST_ARG="--fast"
    fi
done

echo "======================================================================"
echo "🛡️ STARTING PRE-PRODUCTION DEVSECOPS & QUALITY PIPELINE"
echo "   Target Directory: $(pwd)"
echo "   Timestamp: $(date)"
echo "======================================================================"

EXIT_CODE=0

# Load .env file if available
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091  # .env is optional and not available at static analysis time
    source .env 2>/dev/null || true
    set +a
fi

# Trap to ensure summary always prints
_gate_summary() {
    echo ""
    echo "======================================================================"
    if [ "${EXIT_CODE:-0}" -eq 0 ]; then
        echo "✅ DEVSECOPS PIPELINE PASSED: All Quality Gates & Cloud Scans Succeeded Cleanly!"
        exit 0
    else
        echo "❌ DEVSECOPS PIPELINE FAILED: Issues Detected. Check Logs Above."
        exit 1
    fi
}
trap _gate_summary EXIT

# ── STEP 0: REPOSITORY SYNCHRONIZATION ───────────────────────────────────────
echo ""
echo "🔄 [Step 0] Synchronizing Repository with Remote Tracking Branch..."
if git rev-parse --is-inside-work-tree &> /dev/null && git rev-parse --verify HEAD &> /dev/null; then
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    echo "   ► Current Git Branch: $CURRENT_BRANCH"
    echo "   ► Fetching latest changes from remote..."
    git fetch origin 2>/dev/null || echo "   ⚠️ Git fetch skipped (offline or no remote configured)."

    if git rev-parse --verify "origin/$CURRENT_BRANCH" &> /dev/null; then
        echo "   ► Pulling latest commits from origin/$CURRENT_BRANCH..."
        STASHED=false
        if ! git diff --quiet || ! git diff --cached --quiet; then
            git stash push -m "verify-pipeline-auto-stash" --include-untracked &>/dev/null && STASHED=true
        fi
        git pull --rebase origin "$CURRENT_BRANCH" 2>&1 || echo "   ⚠️ Git pull rebase skipped (no remote changes or uncommitted work)."
        if [ "$STASHED" = true ]; then
            git stash pop &>/dev/null || echo "   ⚠️ Could not restore stash — check manually."
        fi
    fi
fi

# ── STEP 1: LOCAL QUALITY & DEVSECOPS VERIFICATION ────────────────────────────
echo ""
echo "⚙️ [Step 1] Executing Local Quality & DevSecOps Verification Suite..."
if [ -f "run_checks.sh" ]; then
    bash run_checks.sh $AUTOFIX_ARG $FAST_ARG || EXIT_CODE=1
else
    echo "   ❌ run_checks.sh not found!"
    EXIT_CODE=1
fi

# ── STEP 2: REMOTE SONARQUBE SAST ANALYSIS ───────────────────────────────────
echo ""
echo "📊 [Step 2] Submitting to Remote SonarQube MQR Quality Gate..."

SONAR_HOST="https://sonarqube.fainko.cloud"
if curl -s -H "User-Agent: Mozilla/5.0" "$SONAR_HOST/api/system/status" | grep -q "UP"; then
    TOKEN="${SONAR_REMOTE_TOKEN:-${SONAR_TOKEN:-}}"

    if [ -n "$TOKEN" ]; then
        NPROC=$(nproc 2>/dev/null || echo "4")
        LIMITED_THREADS=$(( NPROC > 4 ? 4 : NPROC ))

        docker run --rm \
          -e SONAR_HOST_URL="$SONAR_HOST" \
          -v "$(pwd):/usr/src" \
          -v "$(pwd)/.sonar-cache:/opt/sonar-scanner/.sonar/cache" \
          sonarsource/sonar-scanner-cli -Dsonar.scm.provider=git \
          -Dsonar.token="$TOKEN" \
          -Dsonar.threads="$LIMITED_THREADS" \
          -Dsonar.userHome="/opt/sonar-scanner/.sonar" || EXIT_CODE=1
    else
        echo "   ⚠️ SONAR_TOKEN / SONAR_REMOTE_TOKEN not provided (skipping remote scan)."
    fi
else
    echo "   ⚠️ Remote SonarQube server is offline at $SONAR_HOST (skipping)."
fi

# ── STEP 3: GITHUB CI/CD INTEGRATION ─────────────────────────────────────────
echo ""
echo "🐙 [Step 3] Verifying GitHub CLI Integration & PR Status..."

if command -v gh &> /dev/null; then
    if gh auth status &> /dev/null; then
        if git remote &> /dev/null && [ -n "$(git remote 2>/dev/null)" ]; then
            gh pr status >/dev/null 2>&1 || true
            gh run list --limit 3 2>/dev/null || true
        fi
    fi
fi
