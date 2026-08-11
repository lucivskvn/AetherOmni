#!/bin/bash
# ==============================================================================
# GCP CLOUDBUILD & CLOUDRUN AUTOMATED INFRASTRUCTURE PROVISIONER
# ==============================================================================
# Usage: bash scripts/provision_gcp.sh [--submit]
# Description: Dynamically detects GCP Project ID and Project Number from local
#              .env file / gcloud CLI config. STRICT MODE: Requires .env file
#              and fails fast if required secrets are missing without hardcoded fallbacks.
# ==============================================================================

set -eo pipefail

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

# Require .env file — fail fast if missing
if [ ! -f ".env" ]; then
    echo -e "${RED}❌ ERROR: .env file not found at project root.${NC}"
    echo -e "   Please create a .env file with your production parameters before provisioning."
    exit 1
fi

echo -e " -> Loading environment configuration from .env..."
set -o allexport
# shellcheck disable=SC1091
source .env
set +o allexport

# Auto-detect GCP Project ID from .env environment variable or active gcloud CLI config
PROJECT_ID="${GCP_PROJECT_ID:-${GOOGLE_CLOUD_PROJECT:-${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}}}"

if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" == "(unset)" ]; then
    echo -e "${RED}❌ ERROR: GCP Project ID could not be detected.${NC}"
    echo -e "   Please define GCP_PROJECT_ID in .env or run: ${CYAN}gcloud config set project <YOUR_PROJECT_ID>${NC}"
    exit 1
fi

REGION="${GCP_REGION:-asia-southeast1}"
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null || true)

if [ -z "$PROJECT_NUMBER" ]; then
    echo -e "${RED}❌ ERROR: Unable to retrieve project number for '$PROJECT_ID'. Check authentication.${NC}"
    exit 1
fi

COMPUTE_SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
BUILD_SA="${PROJECT_NUMBER}@cloudbuild.gserviceaccount.com"

echo -e "${CYAN}======================================================================${NC}"
echo -e "${CYAN}🚀 GCP AUTOMATED INFRASTRUCTURE PROVISIONER (STRICT .ENV MODE)${NC}"
echo -e "   Project ID     : ${GREEN}${PROJECT_ID}${NC}"
echo -e "   Project Number : ${GREEN}${PROJECT_NUMBER}${NC}"
echo -e "   Region         : ${GREEN}${REGION}${NC}"
echo -e "${CYAN}======================================================================${NC}"

# 1. Enable Required GCP APIs
echo -e "\n${YELLOW}[Step 1/5] Enabling GCP Service APIs...${NC}"
APIS=(
    "cloudbuild.googleapis.com"
    "run.googleapis.com"
    "secretmanager.googleapis.com"
    "artifactregistry.googleapis.com"
    "iam.googleapis.com"
    "cloudtasks.googleapis.com"
    "aiplatform.googleapis.com"
)
for api in "${APIS[@]}"; do
    echo " -> Enabling $api..."
    gcloud services enable "$api" --project="$PROJECT_ID" &>/dev/null || true
done
echo -e "${GREEN}✓ All GCP APIs enabled (including Vertex AI).${NC}"

# 2. Ensure Artifact Registry Repository
echo -e "\n${YELLOW}[Step 2/5] Provisioning Artifact Registry Repository...${NC}"
REPO_NAME="cloud-run-source-deploy"
if ! gcloud artifacts repositories describe "$REPO_NAME" --location="$REGION" --project="$PROJECT_ID" &>/dev/null; then
    echo " -> Creating repository $REPO_NAME in $REGION..."
    gcloud artifacts repositories create "$REPO_NAME" \
        --repository-format=docker \
        --location="$REGION" \
        --description="Cloud Run Source Deployments" \
        --project="$PROJECT_ID"
fi
echo -e "${GREEN}✓ Artifact Registry '$REPO_NAME' is ready.${NC}"

# 3. Provision & Merge Secret Manager Entries (No Hardcoded Fallbacks)
echo -e "\n${YELLOW}[Step 3/5] Provisioning & Merging Secret Manager Secrets from .env...${NC}"

declare -A SECRETS=(
    ["SURREAL_URL"]="$SURREAL_URL"
    ["SURREAL_USER"]="$SURREAL_USER"
    ["SURREAL_PASS"]="$SURREAL_PASS"
    ["ADMIN_EMAIL"]="$ADMIN_EMAIL"
    ["DJANGO_SECRET_KEY"]="$DJANGO_SECRET_KEY"
    ["GEMINI_API_KEY"]="$GEMINI_API_KEY"
    ["SUPABASE_URL"]="$SUPABASE_URL"
    ["SUPABASE_PUBLIC_KEY"]="$SUPABASE_PUBLIC_KEY"
    ["CF_TURNSTILE_SITE_KEY"]="$CF_TURNSTILE_SITE_KEY"
)

MISSING_KEYS=()
for sec in "${!SECRETS[@]}"; do
    val="${SECRETS[$sec]}"
    if [ -z "$val" ]; then
        # Check if secret already exists in GCP Secret Manager to merge
        if ! gcloud secrets describe "$sec" --project="$PROJECT_ID" &>/dev/null; then
            MISSING_KEYS+=("$sec")
        fi
    fi
done

if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
    echo -e "${RED}❌ ERROR: Missing required secrets in .env file:${NC}"
    for k in "${MISSING_KEYS[@]}"; do
        echo -e "   - ${YELLOW}$k${NC}"
    done
    echo -e "Please define these secrets in your .env file before running provisioning."
    exit 1
fi

for sec in "${!SECRETS[@]}"; do
    val="${SECRETS[$sec]}"
    if ! gcloud secrets describe "$sec" --project="$PROJECT_ID" &>/dev/null; then
        echo " -> Creating secret $sec..."
        gcloud secrets create "$sec" --replication-policy=automatic --project="$PROJECT_ID"
    fi
    if [ -n "$val" ]; then
        current_val=$(gcloud secrets versions access latest --secret="$sec" --project="$PROJECT_ID" 2>/dev/null || echo "")
        if [ "$current_val" != "$val" ]; then
            echo " -> Merging & updating secret $sec version from .env..."
            echo -n "$val" | gcloud secrets versions add "$sec" --data-file=- --project="$PROJECT_ID" &>/dev/null
        fi
    else
        echo " -> Merging existing Secret Manager secret: $sec"
    fi
done
echo -e "${GREEN}✓ Secret Manager secrets provisioned and merged.${NC}"

# 4. Bind IAM Roles for Service Accounts (including Vertex AI ADC access)
echo -e "\n${YELLOW}[Step 4/5] Binding IAM Roles & Vertex AI ADC Permissions...${NC}"
ROLES=(
    "roles/run.admin"
    "roles/iam.serviceAccountUser"
    "roles/secretmanager.secretAccessor"
    "roles/storage.admin"
    "roles/aiplatform.user"
)

for sa in "$COMPUTE_SA" "$BUILD_SA"; do
    echo " -> Granting roles (including Vertex AI ADC) to $sa..."
    for role in "${ROLES[@]}"; do
        gcloud projects add-iam-policy-binding "$PROJECT_ID" \
            --member="serviceAccount:$sa" \
            --role="$role" &>/dev/null || true
    done
done

for sec in "${!SECRETS[@]}"; do
    gcloud secrets add-iam-policy-binding "$sec" \
        --member="serviceAccount:$COMPUTE_SA" \
        --role="roles/secretmanager.secretAccessor" \
        --project="$PROJECT_ID" &>/dev/null || true
done
echo -e "${GREEN}✓ IAM roles (including Vertex AI ADC) & secret permissions bound.${NC}"

# 5. Custom Domain Mapping Option (if CUSTOM_DOMAIN is defined in .env)
if [ -n "$CUSTOM_DOMAIN" ]; then
    echo -e "\n${YELLOW}[Step 5/6] Provisioning Custom Domain Mapping for '$CUSTOM_DOMAIN'...${NC}"
    if ! gcloud beta run domain-mappings describe --domain="$CUSTOM_DOMAIN" --region="$REGION" --project="$PROJECT_ID" &>/dev/null; then
        echo " -> Mapping custom domain $CUSTOM_DOMAIN to Cloud Run service 'aether-web'..."
        gcloud beta run domain-mappings create \
            --service="aether-web" \
            --domain="$CUSTOM_DOMAIN" \
            --region="$REGION" \
            --project="$PROJECT_ID" || true
    else
        echo -e "${GREEN}✓ Custom domain '$CUSTOM_DOMAIN' mapping already configured.${NC}"
    fi
fi

# 6. Cloud Build Trigger Option
echo -e "\n${YELLOW}[Step 6/6] Checking Build Submit Flag...${NC}"
if [[ "$1" == "--submit" ]]; then
    echo -e "${CYAN}Submitting build to Cloud Build for project '$PROJECT_ID'...${NC}"
    gcloud builds submit --config cloudbuild.yaml --project="$PROJECT_ID"
else
    echo -e "${GREEN}✓ Provisioning completed! Run 'gcloud builds submit --config cloudbuild.yaml --project=$PROJECT_ID' to deploy.${NC}"
fi
