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

# --- 3. Create the ACR first, so the app image can be built into it ---
# This used to be ONE full apply started in the background, with both image
# builds racing alongside it. The comment justifying that read:
#
#     the Container App is the last resource (gated behind APIM ~30+ min), by
#     which point the ~4 min build is long done and the tag is present in ACR
#
# That was true of the classic APIM tiers. It stopped being true when the
# project moved to StandardV2, which this repo adopted for Anthropic token
# metering and DEPLOYMENT.zh.md celebrates as "约 1–2 分钟建成" — measured at
# 1m32s on a fresh deploy. The safety margin the parallelism rested on went from
# thirty minutes to ninety seconds, while the app image still takes ~7 minutes
# (it builds the React portal with node before the Python layer).
#
# So terraform reached the Container App with the tag not yet pushed, and Azure
# refused it:
#
#     MANIFEST_UNKNOWN: manifest tagged by "v20260902115851" is not found
#
# That is not a retryable error. The app is left provisioningState=Failed with
# no revision, `terraform import` rejects it ("has not been provisioned
# successfully"), and bootstrap.sh exits before create-deployer-sp.sh — a
# half-built environment on a customer's first run. Two consecutive fresh
# deploys (2026-09-02) failed here, so this is now the normal outcome rather
# than an unlucky one.
#
# The fix is to stop racing on the ONE image the Container App needs. The ACR
# apply is seconds; the app build then blocks. The hub image stays in the
# background because nothing in this apply consumes it — it is pulled later, by
# the GitHub Action that deploys a per-account hub.
log "terraform apply (1/2) — ACR only, so there is somewhere to push the image"
terraform apply -input=false -auto-approve \
  -target=module.acr.azurerm_container_registry.acr \
  -var "image_tag=${TAG}" -var "hub_image_tag=${TAG}" \
  || die "terraform apply (ACR) failed — see output above"

ACR_LOGIN_SERVER="$(terraform output -raw acr_login_server 2>/dev/null || true)"
[[ -n "$ACR_LOGIN_SERVER" ]] || die "ACR was applied but acr_login_server is empty"
ACR_NAME="${ACR_LOGIN_SERVER%%.*}"   # strip .azurecr.io

if [[ "$SKIP_BUILD" != "true" ]]; then
  # --- 4. Build the images ---
  # The APP image is built in the FOREGROUND: the Container App created by the
  # apply below refers to it by tag, and Azure resolves that tag while
  # provisioning the first revision. Anything less than "already pushed" is the
  # race this section exists to remove.
  #
  # The HUB image goes to the background and is only waited on at the end. No
  # resource in this apply references it — HUB_IMAGE_REF is published to GitHub
  # Actions later, and the per-account hub deploy pulls it then. Keeping it
  # parallel costs nothing and preserves most of the wall-clock the old shape
  # was after.
  log "Building hub image in the background"
  ( az acr build -r "$ACR_NAME" -t "gitmodel:${TAG}" "$REPO_ROOT/vendored/gitmodel-hub" ) &
  BUILD_HUB_PID=$!

  log "Building app image (blocking — the Container App cannot be created without it)"
  az acr build -r "$ACR_NAME" -t "tokenfoundry:${TAG}" "$REPO_ROOT" \
    || { kill "$BUILD_HUB_PID" 2>/dev/null; die "app image build failed"; }
else
  log "Skipping build (reusing existing images tagged ${TAG})"
  BUILD_HUB_PID=""
fi

# --- 5. Full apply. The app image is in ACR by now, so the Container App can
#        actually start. ---
log "terraform apply (2/2) — everything else (APIM is the long pole)"
terraform apply -input=false -auto-approve \
  -var "image_tag=${TAG}" -var "hub_image_tag=${TAG}" \
  || die "terraform apply failed — see output above"

# The hub image is only needed by later account deploys, but a failure here
# still has to surface: HUB_IMAGE_REF would be published pointing at a tag that
# was never pushed, and the error would resurface minutes later inside a GitHub
# Actions run with no obvious link back to this deploy.
if [[ -n "${BUILD_HUB_PID:-}" ]]; then
  log "Waiting for the hub image build"
  wait "$BUILD_HUB_PID" || die "hub image build failed — see output above"
fi

# Stop the token refresher now that the long apply is done.
[[ "${TOKEN_REFRESH_PID:-}" ]] && kill "$TOKEN_REFRESH_PID" 2>/dev/null || true

# --- 7. Make sure the app is actually ON the image we just built ---
# Terraform applies image_tag only when it CREATES the Container App: the image
# field is under `ignore_changes` so that update-app.sh's out-of-band revisions
# are not reverted on the next apply (see terraform/modules/containerapps).
#
# The consequence, which bit a real dev-16 re-run: if the app already existed —
# a retry after a transient apply failure, or a second deploy.sh on a live
# environment — terraform leaves the OLD image in place and this script would
# otherwise report "deploy complete" while the code it just built was never
# rolled out. So own the image here, the same way update-app.sh does, and keep
# terraform out of it entirely.
ACA_NAME="$(terraform output -raw app_name 2>/dev/null || true)"
ACR_LOGIN="$(terraform output -raw acr_login_server 2>/dev/null || true)"
APP_RG="$(terraform output -raw resource_group 2>/dev/null || true)"
if [[ -n "$ACA_NAME" && -n "$ACR_LOGIN" && -n "$APP_RG" ]]; then
  CURRENT_IMAGE="$(az containerapp show -g "$APP_RG" -n "$ACA_NAME" \
    --query "properties.template.containers[0].image" -o tsv 2>/dev/null || true)"
  WANT_IMAGE="${ACR_LOGIN}/tokenfoundry:${TAG}"
  if [[ "$CURRENT_IMAGE" != "$WANT_IMAGE" ]]; then
    log "Rolling Container App onto ${WANT_IMAGE} (was: ${CURRENT_IMAGE:-none})"
    az containerapp update -g "$APP_RG" -n "$ACA_NAME" --image "$WANT_IMAGE" -o none \
      || die "az containerapp update failed — see output above"
  else
    log "Container App already on tokenfoundry:${TAG}"
  fi
fi

# --- 8. Smoke test ---
APP_FQDN="$(terraform output -raw app_fqdn)"
log "Smoke test: https://${APP_FQDN}/healthz"
if curl -fsS -m 30 "https://${APP_FQDN}/healthz"; then
  printf '\n\033[1;32mDeploy complete — %s is live on tokenfoundry:%s\033[0m\n' "$APP_FQDN" "$TAG"
else
  printf '\n\033[1;33mDeploy applied, but healthz not yet ready (new revision may still be starting).\033[0m\n'
  printf '  Re-check: curl https://%s/healthz\n' "$APP_FQDN"
fi
