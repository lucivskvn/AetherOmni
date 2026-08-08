#!/bin/bash
# =====================================================================
# SonarQube Remote Scanner Launcher
# Target: https://sonarqube.fainko.cloud (self-hosted via Coolify/Cloudflare Tunnel)
# Project key: aetheromni
# =====================================================================
# NOTE: This script is a manual fallback for local submission.
# The primary scan path is the GitHub Actions CI pipeline (.github/workflows/ci.yml)
# which submits automatically on every push/PR.
#
# Prerequisites:
#   - Docker installed and running
#   - SONAR_TOKEN env var set (or enter interactively below)
#   - coverage.xml must exist (run `bash run_checks.sh` first)
# =====================================================================

SONAR_HOST="https://sonarqube.fainko.cloud"
SONAR_PROJECT_KEY="aetheromni"

echo "========================================================"
echo "  AetherOmni — SonarQube Remote Scanner"
echo "  Host   : $SONAR_HOST"
echo "  Project: $SONAR_PROJECT_KEY"
echo "========================================================"
echo ""

# Use env var first, prompt only if not set
if [ -z "$SONAR_TOKEN" ]; then
    read -rp "Enter your SonarQube Project Token: " SONAR_TOKEN
fi

if [ -z "$SONAR_TOKEN" ]; then
    echo ""
    echo "[ERROR] Token cannot be empty. Generate one at: $SONAR_HOST/account/security"
    echo ""
    exit 1
fi

# Verify coverage.xml exists (required for coverage import)
if [ ! -f "coverage.xml" ]; then
    echo "[WARN] coverage.xml not found. Run 'bash run_checks.sh' first to generate it."
    echo "       Continuing without coverage data..."
fi

echo ""
echo "Submitting to SonarQube remote server..."
echo "Please wait — this may take 1-3 minutes..."
echo ""

# Limit worker threads to prevent CPU thrashing (matches CI pipeline setting)
NPROC=$(nproc 2>/dev/null || echo "4")
LIMITED_THREADS=$(( NPROC > 4 ? 4 : NPROC ))

# Run SonarScanner in Docker, mounted to current directory
docker run --rm \
  -e SONAR_HOST_URL="$SONAR_HOST" \
  -v "$(pwd):/usr/src" \
  -v "$(pwd)/.sonar-cache:/opt/sonar-scanner/.sonar/cache" \
  sonarsource/sonar-scanner-cli \
  -Dsonar.token="$SONAR_TOKEN" \
  -Dsonar.scm.disabled=true \
  -Dsonar.threads="$LIMITED_THREADS" \
  -Dsonar.analysis.cache.enabled=true \
  -Dsonar.userHome="/opt/sonar-scanner/.sonar"

echo ""
echo "========================================================"
echo "  Scan submitted!"
echo "  View results: $SONAR_HOST/dashboard?id=$SONAR_PROJECT_KEY"
echo "========================================================"
echo ""
