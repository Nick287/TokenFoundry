#!/usr/bin/env bash
#
# Token Foundry — build & deploy.
#
# This script ORCHESTRATES two distinct concerns (it is NOT itself Terraform):
#   1. Build the app image       (a CI action — `az acr build`)
#   2. Deploy the infrastructure (Terraform consumes that image via -var app_image)
#
# Terraform manages infrastructure STATE; this bash script sequences the
# one-shot ACTIONS around it. The image tag is passed INTO Terraform — Terraform
# consumes the image, it does not build it.
#
# Usage:
#   ./scripts/deploy.sh                  # full deploy; tag auto-generated from timestamp
#   ./scripts/deploy.sh v2               # full deploy with an explicit image tag
#   ./scripts/deploy.sh v2 --skip-build  # re-run Terraform only, reuse existing image
#
# Prereqs: `az login` done, correct subscription selected, secrets exported as
# TF_VAR_pg_admin_password / TF_VAR_jwt_secret / TF_VAR_admin_password
# (or present in terraform/terraform.tfvars).
#
set -euo pipefail

# --- Resolve paths (script lives in scripts/, terraform in terraform/) ---
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TF_DIR="$REPO_ROOT/terraform"

# --- Args ---
TAG="${1:-v$(date +%Y%m%d%H%M%S)}"   # explicit tag, or timestamp-based
SKIP_BUILD=false
[[ "${2:-}" == "--skip-build" ]] && SKIP_BUILD=true

cd "$TF_DIR"

log() { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

# --- Preflight: tools + auth ---
command -v az        >/dev/null || die "az CLI not found"
command -v terraform >/dev/null || die "terraform not found"
az account show >/dev/null 2>&1 || die "Not logged in. Run: az login && az account set --subscription <id>"

# --- 1. terraform init (idempotent; safe to re-run) ---
log "terraform init"
terraform init -input=false >/dev/null

# --- 2. Keep the Azure token fresh for the whole (long) apply ---
# APIM alone can take 30-75+ min. If the az token expires mid-apply, later
# resources (Key Vault secrets, PostgreSQL) fail 401/403. Refresh once now, then
# refresh every 5 min in the background so the az CLI token cache the azurerm
# provider reads stays valid. The trap stops the refresher whenever we exit.
log "Refreshing Azure token, and keeping it fresh during the apply"
az account get-access-token --output none 2>/dev/null || true
( while true; do sleep 300; az account get-access-token --output none 2>/dev/null || true; done ) &
TOKEN_REFRESH_PID=$!
trap '[[ "${TOKEN_REFRESH_PID:-}" ]] && kill "$TOKEN_REFRESH_PID" 2>/dev/null || true' EXIT

# --- 3. Start the single, full terraform apply ---
# ONE apply builds everything, incl. the Container App. Terraform assembles the
# image ref itself as <acr-login-server>/tokenfoundry:<image_tag>, so we only
# pass the tag — no fake placeholder, no second apply. It runs in the background
# so the image build (step 5) can happen in parallel: the Container App is the
# last resource (gated behind APIM ~30+ min), by which point the ~4 min build is
# long done and the tag is present in ACR. Key Vault access is granted inside
# Terraform (keyvault module), so no manual role assignment here.
log "terraform apply START — provisioning all infrastructure (APIM is the long pole)"
terraform apply -input=false -auto-approve -var "image_tag=${TAG}" &
APPLY_PID=$!

# --- 4. Wait for ACR to come up (it only depends on the RG, so ~seconds) ---
# No -target pre-create: the single apply above creates ACR early. Poll its
# output until ready, then we have somewhere to push the image.
if [[ "$SKIP_BUILD" != "true" ]]; then
  log "Waiting for ACR to be created by the apply..."
  ACR_LOGIN_SERVER=""
  for _ in $(seq 1 60); do
    ACR_LOGIN_SERVER="$(terraform output -raw acr_login_server 2>/dev/null || true)"
    [[ -n "$ACR_LOGIN_SERVER" ]] && break
    kill -0 "$APPLY_PID" 2>/dev/null || die "terraform apply exited before ACR was created — see output above"
    sleep 10
  done
  [[ -n "$ACR_LOGIN_SERVER" ]] || die "ACR did not appear within timeout"
  ACR_NAME="${ACR_LOGIN_SERVER%%.*}"   # strip .azurecr.io

  # --- 5. Build images IN PARALLEL while the apply continues toward APIM ---
  # Two images share this ACR: the control-plane app, and the pre-built GitModel
  # hub. The hub is built ONCE here (not per deploy) and referenced per-account by
  # the GitHub Action's terraform (方案 A: the Action deploys hubs, not this
  # script). No deploy-job image any more — 方案 A runs terraform in the Action.
  log "Building app + hub images IN PARALLEL"
  ( az acr build -r "$ACR_NAME" -t "tokenfoundry:${TAG}" "$REPO_ROOT" ) &
  BUILD_APP_PID=$!
  # hub image: build context is the vendored hub root (has Dockerfile + hub/ +
  # requirements.txt). The Action's per-account terraform references gitmodel:<tag>
  # via its HUB_IMAGE_REF repo var (set by scripts/setup-github-deploy.sh).
  ( az acr build -r "$ACR_NAME" -t "gitmodel:${TAG}" "$REPO_ROOT/vendored/gitmodel-hub" ) &
  BUILD_HUB_PID=$!
  wait "$BUILD_APP_PID" || { kill "$APPLY_PID" 2>/dev/null; die "app image build failed"; }
  wait "$BUILD_HUB_PID" || { kill "$APPLY_PID" 2>/dev/null; die "hub image build failed"; }
  log "All images built; apply continues toward the Container App"
else
  log "Skipping build (reusing existing image tokenfoundry:${TAG})"
fi

# --- 6. Wait for the full apply to finish (APIM is the long pole) ---
log "Waiting for terraform apply to complete..."
wait "$APPLY_PID" || die "terraform apply failed — see output above"

# Stop the token refresher now that the long apply is done.
[[ "${TOKEN_REFRESH_PID:-}" ]] && kill "$TOKEN_REFRESH_PID" 2>/dev/null || true

# --- 7. Smoke test ---
APP_FQDN="$(terraform output -raw app_fqdn)"
log "Smoke test: https://${APP_FQDN}/healthz"
if curl -fsS -m 30 "https://${APP_FQDN}/healthz"; then
  printf '\n\033[1;32mDeploy complete — %s is live on tokenfoundry:%s\033[0m\n' "$APP_FQDN" "$TAG"
else
  printf '\n\033[1;33mDeploy applied, but healthz not yet ready (new revision may still be starting).\033[0m\n'
  printf '  Re-check: curl https://%s/healthz\n' "$APP_FQDN"
fi
