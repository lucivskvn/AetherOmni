#!/bin/bash
# =====================================================================
# SonarQube Local Code Scanner Launcher (Linux CLI)
# =====================================================================

echo "========================================================"
echo "  Starting SonarQube Local Code Quality Scan"
echo "========================================================"
echo ""
echo "Server Dashboard: http://localhost:9000"
echo ""
echo "Ensure your SonarQube server is up and running."
echo "To spin it up, run: docker compose -f docker-compose.sonar.yml up -d"
echo ""

# Read the analysis token
read -p "Enter your SonarQube Project Token: " SONAR_TOKEN

if [ -z "$SONAR_TOKEN" ]; then
    echo ""
    echo "[ERROR] Token cannot be empty. Get your token from http://localhost:9000"
    echo ""
    exit 1
fi

echo ""
echo "Running Sonar Scanner container..."
echo "Please wait, analyzing files..."
echo ""

# Run Sonar Scanner mounted to current directory
docker run --rm \
  --add-host=host.docker.internal:host-gateway \
  -e SONAR_HOST_URL="http://host.docker.internal:9000" \
  -v "$(pwd):/usr/src" \
  sonarsource/sonar-scanner-cli \
  -Dsonar.token="$SONAR_TOKEN"

echo ""
echo "========================================================"
echo "  Scan completed!"
echo "  View results: http://localhost:9000/dashboard?id=django-data-extractor"
echo "========================================================"
echo ""
