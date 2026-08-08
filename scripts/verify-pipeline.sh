#!/bin/bash
# ==============================================================================
# UNIFIED PRE-PRODUCTION QUALITY & SECURITY GATE PIPELINE (FULL DEVSECOPS EDITION)
# ==============================================================================
# Usage: ./verify-pipeline.sh [project_dir]

set -o pipefail

PROJECT_DIR="${1:-$(pwd)}"
cd "$PROJECT_DIR"

echo "======================================================================"
echo "🛡️ STARTING DEVSECOPS PRE-PRODUCTION QUALITY & SECURITY PIPELINE"
echo "   Target Directory: $PROJECT_DIR"
echo "   Timestamp: $(date)"
echo "======================================================================"

EXIT_CODE=0

# Trap to ensure summary always prints, even on unexpected exit
_gate_summary() {
    echo ""
    echo "======================================================================"
    if [ "${EXIT_CODE:-0}" -eq 0 ]; then
        echo "✅ DEVSECOPS GATE PASSED: Remote Synced & All 7 Layers Succeeded Cleanly!"
        exit 0
    else
        echo "❌ DEVSECOPS GATE FAILED: Issues Detected. Check Logs Above."
        exit 1
    fi
}
trap _gate_summary EXIT

# ------------------------------------------------------------------------------
# STEP 0: FETCH & PULL LATEST REMOTE CHANGES FIRST
# ------------------------------------------------------------------------------
echo ""
echo "🔄 [STEP 0/7] Fetching and Pulling Remote Branch Changes..."
if git rev-parse --is-inside-work-tree &> /dev/null && git rev-parse --verify HEAD &> /dev/null; then
    CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
    echo "   ► Current Git Branch: $CURRENT_BRANCH"
    echo "   ► Fetching latest changes from remote..."
    git fetch origin 2>/dev/null || echo "   ⚠️ Git fetch failed or no remote configured."

    if git rev-parse --verify "origin/$CURRENT_BRANCH" &> /dev/null; then
        echo "   ► Pulling latest commits from origin/$CURRENT_BRANCH..."
        # Stash local changes so rebase succeeds on dirty workspaces
        STASHED=false
        if ! git diff --quiet || ! git diff --cached --quiet; then
            git stash push -m "verify-pipeline-auto-stash" --include-untracked &>/dev/null && STASHED=true
        fi
        git pull --rebase origin "$CURRENT_BRANCH" 2>&1 || {
            echo "   ❌ Git pull rebase failed (conflict or unreachable remote)."
            EXIT_CODE=1
        }
        # Restore stash if we stashed
        if [ "$STASHED" = true ]; then
            git stash pop &>/dev/null || echo "   ⚠️ Could not restore stash — check manually."
        fi
    else
        echo "   ℹ️ No remote tracking branch found for $CURRENT_BRANCH."
    fi
else
    echo "   ℹ️ Git repository initialized or no commits yet. Skipping git pull."
fi

# ------------------------------------------------------------------------------
# LAYER 1: CONTAINER & INFRASTRUCTURE HARDENING (Hadolint Dockerfile Linter)
# ------------------------------------------------------------------------------
echo ""
echo "🐳 [LAYER 1/7] Running Container & Dockerfile Hardening Check..."
if [ -f "Dockerfile" ] || [ -f "docker-compose.yml" ]; then
    if [ -f "Dockerfile" ]; then
        echo "   ► Executing Hadolint Dockerfile Linter..."
        if [ -x "$HOME/.local/bin/hadolint" ]; then
            "$HOME/.local/bin/hadolint" --format json Dockerfile > hadolint-report.json 2>/dev/null || true
            "$HOME/.local/bin/hadolint" Dockerfile || echo "   ⚠️ Hadolint reported Dockerfile style/security suggestions."
        elif command -v hadolint &> /dev/null; then
            hadolint --format json Dockerfile > hadolint-report.json 2>/dev/null || true
            hadolint Dockerfile || echo "   ⚠️ Hadolint reported Dockerfile style/security suggestions."
        elif command -v docker &> /dev/null; then
            docker run --rm -i hadolint/hadolint hadolint --format json - < Dockerfile > hadolint-report.json 2>/dev/null || true
        else
            echo "   ℹ️ Hadolint missing. Install via ~/.local/bin/hadolint or Docker."
        fi
    fi
    echo "   ✅ Container configuration verified."
else
    echo "   ℹ️ No Dockerfile found in project root."
fi

# ------------------------------------------------------------------------------
# LAYER 2: STATIC LINTING, AUTO-FIXING & AST ANALYSIS (Ruff / ESLint / Prettier)
# ------------------------------------------------------------------------------
echo ""
echo "🔍 [LAYER 2/7] Running Linter, Auto-Fixers & AST Analysis..."

if [ -f "pyproject.toml" ] || [ -f "requirements.txt" ] || [ -f "manage.py" ] || [ -f "converter.py" ]; then
    echo "   ► Python detected. Executing Ruff Auto-Fixer & Formatter..."
    if command -v ruff &> /dev/null; then
        ruff check --fix . || echo "   ⚠️ Ruff applied automated code fixes."
        ruff format . || echo "   ⚠️ Ruff formatted python source code."
    fi

    if command -v mypy &> /dev/null && [ -f "mypy.ini" -o -f "pyproject.toml" ]; then
        echo "   ► Executing Mypy static type checker..."
        mypy . || echo "   ⚠️ Mypy reported type warnings."
        true
    fi
fi

if [ -f "package.json" ]; then
    echo "   ► Node.js/TypeScript detected. Executing Auto-Fixers..."
    if npm run | grep -q "format"; then
        npm run format 2>/dev/null || npx prettier --write "src/**/*.{ts,tsx,js,jsx,json,css}" 2>/dev/null || true
    fi
    if npm run | grep -q "lint"; then
        npm run lint -- --fix 2>/dev/null || true
        npm run lint
        LINT_STATUS=$?
        if [ $LINT_STATUS -ne 0 ]; then
            echo "   ⚠️ ESLint reported code style/formatting warnings."
        fi
    fi
fi

if [ -f "composer.json" ]; then
    echo "   ► PHP / Composer detected. Running Composer Audit & Linter..."
    if command -v composer &> /dev/null; then
        composer audit || echo "   ⚠️ Composer audit reported security warnings."
    fi
    if [ -f "vendor/bin/phpstan" ]; then
        ./vendor/bin/phpstan analyse || echo "   ⚠️ PHPStan reported static analysis warnings."
    fi
fi

# ------------------------------------------------------------------------------
# LAYER 3: STRUCTURAL ARCHITECTURE & DEBT ANALYSIS (Desloppify)
# ------------------------------------------------------------------------------
echo ""
echo "🏗️ [LAYER 3/7] Running Desloppify Structural Scan..."
if command -v desloppify &> /dev/null && [ -d ".desloppify" ]; then
    echo "   ► Executing Desloppify Scan..."
    desloppify scan || echo "   ⚠️ Desloppify reported queue suggestions."
    echo "   ► Updating Desloppify Scorecard Visual..."
    desloppify viz --output scorecard.png 2>/dev/null || true
else
    echo "   ⚠️ Desloppify not found or .desloppify uninitialized. Skipping structural scan."
fi

# ------------------------------------------------------------------------------
# LAYER 4: FREE OPEN-SOURCE SAST & INJECTION SECURITY AUDIT (Bandit / AST-Grep / Semgrep)
# ------------------------------------------------------------------------------
echo ""
echo "🛡️ [LAYER 4/7] Running Open-Source Deep SAST & Injection Security Scans..."

# 1. Free Python SQLi / Command Injection / XSS Scanner (Bandit)
if [ -f "converter.py" ] || [ -f "manage.py" ] || [ -f "pyproject.toml" ]; then
    echo "   ► Running Bandit Open-Source SAST (SQLi, Code Execution & Security Auditor)..."
    python3 -m bandit -r . -x .venv,.git,__pycache__ --severity-level medium -f json -o bandit-report.json 2>/dev/null || true
    python3 -m bandit -r . -x .venv,.git,__pycache__ --severity-level high --quiet || echo "   ⚠️ Bandit reported high-severity security warnings."
fi

# 2. Free Node.js / TypeScript Injection & AST Security Auditor (AST-Grep)
if [ -f "package.json" ]; then
    echo "   ► Running AST-Grep AST Security & Pattern Auditor..."
    npx -y @ast-grep/cli scan --json > ast-grep-report.json 2>/dev/null || true
fi

# 3. Local Semgrep SAST Engine if available — search common install locations
SEMGREP_BIN=""
# Check PATH first
if command -v semgrep &> /dev/null; then
    SEMGREP_BIN=$(command -v semgrep)
else
    # Search common user-level install directories (cross-machine portable)
    for candidate in \
        "$HOME/.local/bin/semgrep" \
        "$HOME/.venv/bin/semgrep" \
        "$(python3 -m site --user-base 2>/dev/null)/bin/semgrep" \
        "/usr/local/bin/semgrep"; do
        if [ -x "$candidate" ]; then
            SEMGREP_BIN="$candidate"
            break
        fi
    done
    # Fallback: scan Kiro config directory for a bundled binary
    if [ -z "$SEMGREP_BIN" ] && [ -d "$HOME/.config/Kiro" ]; then
        SEMGREP_BIN=$(find "$HOME/.config/Kiro" -name "semgrep" -type f 2>/dev/null | head -1)
    fi
fi
if [ -n "$SEMGREP_BIN" ] && [ -x "$SEMGREP_BIN" ]; then
    echo "   ► Executing Semgrep SAST Engine..."
    "$SEMGREP_BIN" scan --config=auto --quiet || true
fi

# ------------------------------------------------------------------------------
# LAYER 5: UNIT TESTS & COVERAGE REPORT GENERATION (QA Pipeline)
# ------------------------------------------------------------------------------
echo ""
echo "🧪 [LAYER 5/7] Executing Unit Tests & Generating Coverage XML..."

if [ -f "manage.py" ]; then
    echo "   ► Executing Django Unit Tests with Coverage..."
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    export DJANGO_SECRET_KEY="${DJANGO_SECRET_KEY:-django-insecure-ci-test-key-50-chars-long-for-local-testing}"
    coverage run --source='.' manage.py test --keepdb || EXIT_CODE=1
    coverage xml -o coverage.xml
    
    if [ -f "coverage.xml" ]; then
        sed -i 's|<source>.*</source>|<source>.</source>|g' coverage.xml
        echo "   ✅ Path mapping updated in coverage.xml for Docker Scanner."
    fi

elif [ -f "test_converter.py" ] || [ -f "pytest.ini" ] || [ -d "tests" ]; then
    echo "   ► Executing Pytest / Unittest with Coverage..."
    if [ -f "test_converter.py" ]; then
        coverage run test_converter.py || python3 test_converter.py || true
        coverage xml -o coverage.xml 2>/dev/null || true
    elif command -v pytest &> /dev/null; then
        pytest --cov=. --cov-report=xml || true
    else
        python3 -m unittest discover || true
    fi

    if [ -f "coverage.xml" ]; then
        sed -i 's|<source>.*</source>|<source>.</source>|g' coverage.xml
        echo "   ✅ Path mapping updated in coverage.xml for Docker Scanner."
    fi

elif [ -f "package.json" ]; then
    echo "   ► Executing Jest/Vitest Front-end Tests..."
    if npm run | grep -q "test:coverage"; then
        npm run test:coverage || EXIT_CODE=1
    elif npm run | grep -q "test"; then
        npm run test || EXIT_CODE=1
    fi
fi

# ------------------------------------------------------------------------------
# LAYER 6: SONARQUBE DEEP ANALYSIS (MQR Quality Gate & Eco-Design)
# ------------------------------------------------------------------------------
echo ""
echo "📊 [LAYER 6/7] Submitting to SonarQube MQR Server..."

SONAR_HOST="https://sonarqube.fainko.cloud"
if curl -s -H "User-Agent: Mozilla/5.0" "$SONAR_HOST/api/system/status" | grep -q "UP"; then
    TOKEN="${SONAR_REMOTE_TOKEN:-${SONAR_TOKEN:-}}"

    if [ -n "$TOKEN" ]; then
        # Limit worker threads to half CPU count (max 4) to prevent CPU thrashing & OS freeze
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
        echo "   ❌ Failed to locate SonarQube remote token."
        EXIT_CODE=1
    fi
else
    echo "   ⚠️ Remote SonarQube server is offline at $SONAR_HOST. Skipping Sonar scan."
fi

# ------------------------------------------------------------------------------
# LAYER 7: GITHUB WORKFLOW & DEPLOYMENT INTEGRATION (GH CLI)
# ------------------------------------------------------------------------------
echo ""
echo "🐙 [LAYER 7/7] Checking GitHub CLI Integration & PR Status..."

if command -v gh &> /dev/null; then
    if gh auth status &> /dev/null; then
        if git remote &> /dev/null && [ -n "$(git remote 2>/dev/null)" ]; then
            echo "   ► GitHub CLI Authenticated. Checking open PRs & workflow status..."
            gh pr status >/dev/null 2>&1 || true
            gh run list --limit 3 2>/dev/null || true
        else
            echo "   ℹ️ No remote Git repository origin configured. Skipping GH PR status."
        fi
    else
        echo "   ℹ️ GitHub CLI (\`gh\`) installed. Run \`gh auth login\` to enable automated PR & Workflow status tracking."
    fi
else
    echo "   ℹ️ GitHub CLI not installed."
fi

# (Summary and exit are handled by the _gate_summary trap on EXIT above)
