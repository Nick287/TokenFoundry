#!/usr/bin/env bash
#
# Token Foundry — app-only image update (NO infrastructure changes).
#
# Use this when you've changed application code (FastAPI / React portal) and just
# want to ship a new image to an existing environment — WITHOUT running Terraform
# or Bicep. It does exactly three things:
#   1. az acr build   — build+push the image to that environment's ACR (cloud build)
#   2. az containerapp update — roll the Container App onto the new image
#   3. wait for the new revision to be Running, then smoke-test /healthz
#
# It does NOT touch APIM, Cosmos, Key Vault, Postgres, or any Terraform/Bicep
# state. Infrastructure is unchanged; only the Container App's image tag rolls.
# (For a full infra deploy, use ./scripts/deploy.sh instead — that one runs
# `terraform apply`.)
#
# The resource group is REQUIRED (this repo has more than one environment, e.g.
# tokenfoundry-rg and tokenfoundry-rg-dev-02). The ACR and Container App inside
# it are auto-discovered, so you never hardcode the hashed resource names.
#
# Usage:
#   ./scripts/update-app.sh -g tokenfoundry-rg-dev-02
#   ./scripts/update-app.sh -g tokenfoundry-rg-dev-02 -t v7
#   ./scripts/update-app.sh -g tokenfoundry-rg-dev-02 -t v7 --skip-build
#
# Options:
#   -g <rg>        (required) resource group of the target environment
#   -t <tag>       image tag (default: timestamp, e.g. v20260701153000)
#   --skip-build   reuse an existing tag in ACR; only roll the Container App
#   -h             show this help
#
# Prereqs: `az login` done and the correct subscription selected.

set -euo pipefail

# --- pretty output (mirrors deploy.sh) ---
log()  { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# --- args ---
RG=""
TAG="v$(date +%Y%m%d%H%M%S)"
SKIP_BUILD=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g) RG="${2:-}"; shift 2 ;;
    -t) TAG="${2:-}"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (use -h for help)" ;;
  esac
done

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# --- preflight ---
command -v az >/dev/null || die "az CLI not found"
az account show >/dev/null 2>&1 || die "Not logged in. Run: az login && az account set --subscription <id>"
[[ -n "$RG" ]] || die "resource group is required: -g <rg> (e.g. -g tokenfoundry-rg-dev-02)"
az group show -n "$RG" >/dev/null 2>&1 || die "resource group '$RG' not found (or no access)"

# --- discover ACR + Container App in the target RG ---
log "Discovering ACR and Container App in $RG"
ACR_NAME="$(az acr list -g "$RG" --query "[0].name" -o tsv 2>/dev/null || true)"
[[ -n "$ACR_NAME" ]] || die "no Container Registry found in $RG"
ACR_LOGIN_SERVER="$(az acr show -n "$ACR_NAME" --query loginServer -o tsv)"

ACA_NAME="$(az containerapp list -g "$RG" --query "[0].name" -o tsv 2>/dev/null || true)"
[[ -n "$ACA_NAME" ]] || die "no Container App found in $RG"

IMAGE_REF="${ACR_LOGIN_SERVER}/tokenfoundry:${TAG}"
printf '  ACR           : %s\n' "$ACR_NAME"
printf '  Container App : %s\n' "$ACA_NAME"
printf '  Image         : %s\n' "$IMAGE_REF"

# --- 1. build (cloud build in ACR) ---
if [[ "$SKIP_BUILD" != "true" ]]; then
  log "Building image in ACR (cloud build): $IMAGE_REF"
  az acr build -r "$ACR_NAME" -t "tokenfoundry:${TAG}" "$REPO_ROOT" \
    || die "az acr build failed — see output above"
  ok "Image build complete"
else
  log "Skipping build; reusing existing tag $TAG"
  az acr repository show-tags -n "$ACR_NAME" --repository tokenfoundry -o tsv 2>/dev/null \
    | grep -qx "$TAG" || die "tag '$TAG' not found in $ACR_NAME — cannot --skip-build"
fi

# --- 2. roll the Container App onto the new image ---
log "Updating Container App $ACA_NAME to the new image"
NEW_REVISION="$(az containerapp update -g "$RG" -n "$ACA_NAME" \
  --image "$IMAGE_REF" \
  --query "properties.latestRevisionName" -o tsv)" \
  || die "az containerapp update failed — see output above"
printf '  New revision  : %s\n' "$NEW_REVISION"

# --- 3. wait for the new revision to be Running ---
log "Waiting for revision $NEW_REVISION to be Running"
STATE=""
for _ in $(seq 1 60); do
  STATE="$(az containerapp revision show -g "$RG" -n "$ACA_NAME" \
    --revision "$NEW_REVISION" --query "properties.runningState" -o tsv 2>/dev/null || true)"
  case "$STATE" in
    Running) break ;;
    Failed|Degraded) die "revision $NEW_REVISION entered state: $STATE" ;;
  esac
  sleep 5
done
[[ "$STATE" == "Running" ]] || die "revision did not reach Running within timeout (last: ${STATE:-unknown})"
ok "Revision is Running"

# --- 4. smoke test ---
FQDN="$(az containerapp show -g "$RG" -n "$ACA_NAME" \
  --query "properties.configuration.ingress.fqdn" -o tsv)"
log "Smoke test: https://${FQDN}/healthz"
if curl -fsS -m 30 "https://${FQDN}/healthz" >/dev/null; then
  ok "Deploy complete — https://${FQDN} is live on tokenfoundry:${TAG} (revision ${NEW_REVISION})"
else
  warn "Image rolled, but /healthz not ready yet (new revision may still be starting)."
  printf '  Re-check: curl https://%s/healthz\n' "$FQDN"
fi
