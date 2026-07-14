#!/usr/bin/env bash
# Deploy DeepMorph Orchestrator to GCP Cloud Run.
#
# Prerequisites:
#   gcloud auth login
#   gcloud config set project <PROJECT_ID>
#
# Secrets must be pre-created in Secret Manager:
#   gcloud secrets create next_public_supabase_url --data-file=- <<< "https://<project>.supabase.co"
#   gcloud secrets create next_public_supabase_anon_key --data-file=- <<< "<anon-key>"
#   gcloud secrets create supabase_jwt_secret --data-file=- <<< "<jwt-secret>"
#
# Usage:
#   ./deploy.sh [PROJECT_ID] [REGION]

set -euo pipefail

PROJECT_ID="${1:-$(gcloud config get-value project)}"
REGION="${2:-us-central1}"
SERVICE_NAME="ark-mcp"
SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "Regenerating requirements.txt from the local MCP project..."
uv export --project "${SOURCE_DIR}" --no-dev --no-hashes > "${SOURCE_DIR}/requirements.txt"

# Cloud Build (BuildKit) rejects symlinks pointing outside the build context.
# Resolve all symlinks into a temp dir so the context is self-contained.
BUILD_DIR="$(mktemp -d)"
trap 'rm -rf "${BUILD_DIR}"' EXIT
cp -rL "${SOURCE_DIR}/." "${BUILD_DIR}/"

echo "Deploying ${SERVICE_NAME} to Cloud Run (project=${PROJECT_ID}, region=${REGION})"

gcloud run deploy "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --source "${BUILD_DIR}" \
  --allow-unauthenticated \
  --port 8080 \
  --set-secrets \
    "SUPABASE_URL=next_public_supabase_url:latest,\
SUPABASE_ANON_KEY=next_public_supabase_anon_key:latest,\
SUPABASE_JWT_SECRET=supabase_jwt_secret:latest"

echo ""
echo "Deployed. MCP endpoint:"
gcloud run services describe "${SERVICE_NAME}" \
  --project "${PROJECT_ID}" \
  --region "${REGION}" \
  --format "value(status.url)" | sed 's|$|/mcp|'
