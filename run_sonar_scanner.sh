#!/bin/bash
set -euo pipefail

# =====================================================================
# SonarCloud Remote Scanner Launcher
# Target: https://sonarcloud.io
# Organization: lucivskvn
# Project key: lucivskvn_AetherOmni
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

SONAR_HOST="https://sonarcloud.io"
SONAR_PROJECT_KEY="lucivskvn_AetherOmni"
SONAR_ORGANIZATION="lucivskvn"

echo "========================================================"
echo "  AetherOmni — SonarCloud Remote Scanner"
echo "  Host        : $SONAR_HOST"
echo "  Organization: $SONAR_ORGANIZATION"
echo "  Project     : $SONAR_PROJECT_KEY"
echo "========================================================"
echo ""

# Use env var first, prompt only if not set
if [ -z "${SONAR_TOKEN:-}" ]; then
    read -rp "Enter your SonarCloud User Token: " SONAR_TOKEN
fi

if [ -z "${SONAR_TOKEN:-}" ]; then
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
echo "Submitting to SonarCloud remote platform..."
echo "Please wait — this may take 1-3 minutes..."
echo ""

# Limit worker threads to prevent CPU thrashing (matches CI pipeline setting)
NPROC=$(nproc 2>/dev/null || echo "4")
LIMITED_THREADS=$(( NPROC > 4 ? 4 : NPROC ))

# Run SonarScanner in Docker, mounted to current directory
docker run --rm \
  -e SONAR_HOST_URL="$SONAR_HOST" \
  -v "$(pwd):/usr/src" \
  sonarsource/sonar-scanner-cli:latest \
  -Dsonar.token="$SONAR_TOKEN" \
  -Dsonar.organization="$SONAR_ORGANIZATION" \
  -Dsonar.projectKey="$SONAR_PROJECT_KEY" \
  -Dsonar.projectName="AetherOmni" \
  -Dsonar.sources=. \
  -Dsonar.tests=extractor/tests \
  -Dsonar.test.inclusions="extractor/tests/**/*.py" \
  -Dsonar.python.version=3.13 \
  -Dsonar.sourceEncoding=UTF-8 \
  -Dsonar.scm.provider=git \
  -Dsonar.python.coverage.reportPaths=coverage.xml \
  -Dsonar.javascript.lcov.reportPaths=coverage/js/lcov.info \
  -Dsonar.threads="$LIMITED_THREADS" \
  -Dsonar.analysis.cache.enabled=true

echo ""
echo "========================================================"
echo "  Analysis complete!"
echo "  Dashboard: $SONAR_HOST/dashboard?id=$SONAR_PROJECT_KEY"
echo "========================================================"
