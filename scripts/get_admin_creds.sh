#!/bin/bash
# ==============================================================================
# AETHEROMNI ADMIN EMAIL & SUPABASE AUTH CONFIGURATION RETRIEVER
# ==============================================================================
# Usage: bash scripts/get_admin_creds.sh
# ==============================================================================

set -eo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

if [ -f ".env" ]; then
    set -o allexport
    # shellcheck disable=SC1091
    source .env
    set +o allexport
fi

PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" == "(unset)" ]; then
    echo -e "${RED}❌ ERROR: GCP Project ID could not be auto-detected.${NC}"
    echo -e "   Please define GCP_PROJECT_ID in .env or run: ${CYAN}gcloud config set project <YOUR_PROJECT_ID>${NC}"
    exit 1
fi

echo -e "${CYAN}======================================================================${NC}"
echo -e "${CYAN}🔐 AETHEROMNI ADMIN EMAIL & SUPABASE AUTH CONFIGURATION${NC}"
echo -e "   Target GCP Project: ${GREEN}${PROJECT_ID}${NC}"
echo -e "${CYAN}======================================================================${NC}"

fetch_secret() {
    local sec="$1"
    local val
    val=$(gcloud secrets versions access latest --secret="$sec" --project="$PROJECT_ID" 2>/dev/null || true)
    if [ -z "$val" ]; then
        echo "(not set / environment fallback)"
    else
        echo "$val"
    fi
}

ADMIN_MAIL=$(fetch_secret "ADMIN_EMAIL")
SUPABASE_URL_VAL=$(fetch_secret "SUPABASE_URL")
SUPABASE_KEY_VAL=$(fetch_secret "SUPABASE_PUBLIC_KEY")

echo -e "\n${YELLOW}📋 Administrative Account Details:${NC}"
echo -e "  • Email        : ${GREEN}${ADMIN_MAIL}${NC}"

echo -e "\n${YELLOW}⚡ Supabase Auth Integration:${NC}"
echo -e "  • Supabase URL : ${GREEN}${SUPABASE_URL_VAL}${NC}"
if [ "$SUPABASE_KEY_VAL" != "(not set / environment fallback)" ]; then
    echo -e "  • Public Key   : ${GREEN}Configured (${#SUPABASE_KEY_VAL} chars)${NC}"
else
    echo -e "  • Public Key   : ${YELLOW}Not configured (using local Django auth fallback)${NC}"
fi
echo -e "${CYAN}======================================================================${NC}"
