#!/bin/bash
set -eo pipefail

# ANSI styling colors
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0;m'

echo -e "${CYAN}====================================================${NC}"
echo -e "${CYAN}        AetherOmni - Pre-check QA Runner     ${NC}"
echo -e "${CYAN}   Version: $(cat VERSION 2>/dev/null || echo 'unknown')     ${NC}"
echo -e "${CYAN}====================================================${NC}"

# Ensure virtual env is active
if [ -d ".venv" ]; then
    source .venv/bin/activate
fi

echo -e "\n${YELLOW}[Step 1/7] Running Ruff Linter...${NC}"
if command -v ruff &> /dev/null; then
    ruff check .
    echo -e "${GREEN}✓ Linter passed successfully.${NC}"
else
    echo -e "${RED}✗ Ruff is not installed! Please run 'pip install ruff'.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[Step 2/7] Checking Ruff Formatting...${NC}"
ruff format --check .
echo -e "${GREEN}✓ Code formatting is consistent.${NC}"

# Determine python executable
PYTHON_BIN="python"
if [ -f ".venv/bin/python3" ]; then
    PYTHON_BIN=".venv/bin/python3"
elif [ -f ".venv/bin/python" ]; then
    PYTHON_BIN=".venv/bin/python"
fi

# Ensure secret key is available for check runner & local testing
export DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-django-insecure-ci-test-key-50-chars-long-for-local-testing}"

echo -e "\n${YELLOW}[Step 3/7] Running Django System Integrity Checks...${NC}"
$PYTHON_BIN manage.py check
echo -e "${GREEN}✓ Django integrity checks passed.${NC}"

echo -e "\n${YELLOW}[Step 4/7] Executing Django Unit Tests...${NC}"
echo -e "${YELLOW}Running tests in offline mode...${NC}"
SURREALDB_OFFLINE=True DATABASE_URL=sqlite:///db.sqlite3 $PYTHON_BIN manage.py test --keepdb 2>&1 | tee /tmp/test_output.txt
# Cache test count for the auto-doc updater
TEST_COUNT=$(grep -oP '(?<=Ran )\d+' /tmp/test_output.txt | tail -1)
if [ -n "$TEST_COUNT" ]; then echo "$TEST_COUNT" > .test_count; fi
echo -e "${GREEN}✓ All unit tests passed!${NC}"

echo -e "\n${YELLOW}[Step 5/7] Running Bandit Static Security Scan...${NC}"
if $PYTHON_BIN -m bandit -s B310,B110,B112 -c bandit.yaml -r extractor/; then
    echo -e "${GREEN}✓ Bandit static security scan passed (No issues identified).${NC}"
else
    echo -e "${RED}✗ Bandit security scan failed! Please resolve issues above.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[Step 6/7] Running Pip-Audit Dependency Scan...${NC}"
if $PYTHON_BIN -m pip_audit \
    --ignore-vuln PYSEC-2026-2132 \
    --ignore-vuln PYSEC-2026-3545 \
    --ignore-vuln PYSEC-2026-3546 \
    --ignore-vuln PYSEC-2026-3547 \
    --ignore-vuln PYSEC-2026-3552; then
    echo -e "${GREEN}✓ Pip-Audit dependency security check passed.${NC}"
else
    echo -e "${RED}✗ Pip-Audit detected vulnerable dependencies! Please upgrade them.${NC}"
    exit 1
fi

echo -e "\n${YELLOW}[Step 7/7] Auto-updating Documentation...${NC}"
if $PYTHON_BIN scripts/update_docs.py; then
    echo -e "${GREEN}✓ Documentation refreshed successfully.${NC}"
else
    echo -e "${YELLOW}⚠ Documentation update skipped (non-fatal).${NC}"
fi

echo -e "\n${GREEN}====================================================${NC}"
echo -e "${GREEN}   ✓ ALL QUALITY GATES PASSED. READY FOR DEPLOY!   ${NC}"
echo -e "${GREEN}====================================================${NC}"
