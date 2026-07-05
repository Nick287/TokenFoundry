#!/usr/bin/env bash
#
# Token Foundry — one-shot configuration for 方案 A (deploy hubs via GitHub Action).
#
# The per-account GitModel hub terraform runs in the GitHub Action `deploy-hub.yml`
# using Service Principal auth. This script wires up everything that lives OUTSIDE
# terraform (out-of-band), so the control plane can trigger that Action and read
# back its results:
#
#   1. repo SECRETS  (gh secret set)   — the Service Principal creds the Action's
#      terraform authenticates with: ARM_CLIENT_ID / ARM_CLIENT_SECRET /
#      ARM_TENANT_ID / ARM_SUBSCRIPTION_ID.
#   2. repo VARS     (gh variable set) — non-secret infra the Action reuses:
#      HUB_ACR_NAME / HUB_ACR_RG / HUB_LOCATION / HUB_IMAGE_REF /
#      TFSTATE_STORAGE_ACCOUNT / TFSTATE_CONTAINER / HUB_KEYVAULT_NAME.
#   3. KV secret     (az keyvault)     — the GitHub PAT the CONTROL PLANE uses to
#      trigger + poll the workflow, written to `hub-deploy-github-token`.
#
# The SP itself (and its role bundle: Contributor + User Access Administrator +
# Key Vault Secrets User + Storage Blob Data Contributor) is created SEPARATELY by
# `scripts/create-deployer-sp.sh` — run that FIRST. This script only consumes the
# SP's creds (from env) to populate the repo secrets; it does not grant roles.
#
# Most infra values are auto-discovered from `terraform output` (run against the
# target env's state). The Service Principal creds are read from KEY VAULT
# (deployer-sp-* secrets, written by create-deployer-sp.sh); an env var of the
# same name OVERRIDES the KV value. Only the GitHub PAT must come from env:
#
#   ARM_CLIENT_ID          (optional) SP appId — overrides KV deployer-sp-client-id
#   ARM_CLIENT_SECRET      (optional) SP password — overrides KV deployer-sp-client-secret
#   ARM_TENANT_ID          (optional) tenant id — overrides KV deployer-sp-tenant-id
#   ARM_SUBSCRIPTION_ID    (optional) subscription id — overrides KV deployer-sp-subscription-id
#   GITHUB_DEPLOY_PAT      (required) GitHub PAT (fine-grained: target repo Actions
#                          RW + Contents read) the control plane uses to dispatch/poll
#
# Usage (after deploy.sh + create-deployer-sp.sh have run):
#   export GITHUB_DEPLOY_PAT=github_pat_...
#   ./scripts/setup-github-deploy.sh -g tokenfoundry-rg-dev-001
#   ./scripts/setup-github-deploy.sh -g tokenfoundry-rg-dev-02
#
# Options:
#   -g <rg>       (required) resource group of the target environment
#   -r <owner/repo>  GitHub repo (default: inferred from gh / git remote)
#   -h            help
#
# Prereqs: `az login` (subscription selected) + `gh auth login` (or GH_TOKEN set)
# with admin on the repo (needed to set secrets/vars). Run create-deployer-sp.sh
# FIRST — it creates the SP and stores its creds in Key Vault.

set -euo pipefail

log()  { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m%s\033[0m\n' "$*"; }
warn() { printf '\033[1;33m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,48p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

# --- args ---
RG=""
REPO=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g) RG="${2:-}"; shift 2 ;;
    -r) REPO="${2:-}"; shift 2 ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (use -h for help)" ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TF_DIR="$(cd "$SCRIPT_DIR/.." && pwd)/terraform"

# --- preflight: tools + auth ---
command -v az        >/dev/null || die "az CLI not found"
command -v gh        >/dev/null || die "gh CLI not found (https://cli.github.com)"
command -v terraform >/dev/null || die "terraform not found"
az account show >/dev/null 2>&1 || die "Not logged in to Azure. Run: az login"
gh auth status  >/dev/null 2>&1 || die "Not logged in to GitHub. Run: gh auth login"
[[ -n "$RG" ]] || die "resource group is required: -g <rg>"
az group show -n "$RG" >/dev/null 2>&1 || die "resource group '$RG' not found (or no access)"

# --- preflight: required secret in env (never args/hardcoded) ---
# The SP creds (ARM_*) are read from Key Vault below (written there by
# create-deployer-sp.sh); env vars, if set, OVERRIDE the KV values. The GitHub
# PAT has no KV source at this point, so it must come from env.
: "${GITHUB_DEPLOY_PAT:?export GITHUB_DEPLOY_PAT (GitHub PAT for dispatch/poll)}"

# --- resolve target repo ---
if [[ -z "$REPO" ]]; then
  REPO="$(gh repo view --json nameWithOwner -q .nameWithOwner 2>/dev/null || true)"
  [[ -n "$REPO" ]] || die "could not infer repo; pass -r <owner/repo>"
fi
log "Target repo: $REPO   |   env RG: $RG"

# --- discover infra values from terraform output (target env's state) ---
log "Reading infra values from terraform output"
cd "$TF_DIR"
terraform init -input=false >/dev/null 2>&1 || true
tf_out() { terraform output -raw "$1" 2>/dev/null || true; }

TFSTATE_SA="$(tf_out tfstate_storage_account)"
TFSTATE_CT="$(tf_out tfstate_container)"
KV_NAME="$(tf_out keyvault_name)"
ACR_LOGIN="$(tf_out acr_login_server)"

# Fall back to az discovery when terraform state isn't local (e.g. dev-02).
[[ -n "$ACR_LOGIN" ]] || ACR_LOGIN="$(az acr list -g "$RG" --query '[0].loginServer' -o tsv 2>/dev/null || true)"
[[ -n "$KV_NAME"   ]] || KV_NAME="$(az keyvault list -g "$RG" --query '[0].name' -o tsv 2>/dev/null || true)"
[[ -n "$TFSTATE_SA" ]] || TFSTATE_SA="$(az storage account list -g "$RG" --query "[?contains(name,'tfstate')].name | [0]" -o tsv 2>/dev/null || true)"

[[ -n "$ACR_LOGIN"  ]] || die "could not resolve ACR login server (checked tf output + $RG)"
[[ -n "$KV_NAME"    ]] || die "could not resolve Key Vault name (checked tf output + $RG)"
[[ -n "$TFSTATE_SA" ]] || die "could not resolve tfstate storage account (checked tf output + $RG)"
[[ -n "$TFSTATE_CT" ]] || TFSTATE_CT="hub-tfstate"

# --- resolve SP creds: Key Vault first (方案 A), env vars override ---
# create-deployer-sp.sh stored these as deployer-sp-* secrets. An explicit env
# var wins (lets you override without touching KV). A missing value after both
# sources is fatal — setup can't set the repo secrets without it.
log "Reading Service Principal creds from Key Vault $KV_NAME (deployer-sp-*)"
kv_get() { az keyvault secret show --vault-name "$KV_NAME" --name "$1" --query value -o tsv 2>/dev/null || true; }
ARM_CLIENT_ID="${ARM_CLIENT_ID:-$(kv_get deployer-sp-client-id)}"
ARM_CLIENT_SECRET="${ARM_CLIENT_SECRET:-$(kv_get deployer-sp-client-secret)}"
ARM_TENANT_ID="${ARM_TENANT_ID:-$(kv_get deployer-sp-tenant-id)}"
ARM_SUBSCRIPTION_ID="${ARM_SUBSCRIPTION_ID:-$(kv_get deployer-sp-subscription-id)}"
[[ -n "$ARM_CLIENT_ID"       ]] || die "ARM_CLIENT_ID not in KV or env — run create-deployer-sp.sh first"
[[ -n "$ARM_CLIENT_SECRET"   ]] || die "ARM_CLIENT_SECRET not in KV or env — run create-deployer-sp.sh (or --reset-password)"
[[ -n "$ARM_TENANT_ID"       ]] || die "ARM_TENANT_ID not in KV or env"
[[ -n "$ARM_SUBSCRIPTION_ID" ]] || die "ARM_SUBSCRIPTION_ID not in KV or env"
ok "SP creds resolved (client-id ${ARM_CLIENT_ID:0:8}…)"

ACR_NAME="${ACR_LOGIN%%.*}"        # strip .azurecr.io
HUB_LOCATION="$(az group show -n "$RG" --query location -o tsv)"
# The hub image is built by scripts/deploy.sh as gitmodel:<image_tag>. Default to
# :latest; override HUB_IMAGE_REF in env to pin a specific tag.
HUB_IMAGE_REF="${HUB_IMAGE_REF:-gitmodel:latest}"

printf '  ACR (name/rg)      : %s / %s\n' "$ACR_NAME" "$RG"
printf '  Hub location       : %s\n' "$HUB_LOCATION"
printf '  Hub image ref      : %s\n' "$HUB_IMAGE_REF"
printf '  tfstate (sa/ct)    : %s / %s\n' "$TFSTATE_SA" "$TFSTATE_CT"
printf '  Key Vault          : %s\n' "$KV_NAME"

# --- 1. repo SECRETS: the Service Principal creds the Action's terraform uses ---
log "Setting repo secrets (ARM_* Service Principal creds)"
# NOTE: pass the value via --body "$VALUE" directly. Do NOT use `| gh secret set
# --body -`: gh treats `-` as the LITERAL body (a 1-char secret), it does NOT read
# stdin — that silently stored a 1-char tenant/secret and broke azure/login.
gh secret set ARM_CLIENT_ID       --repo "$REPO" --body "$ARM_CLIENT_ID"
gh secret set ARM_CLIENT_SECRET   --repo "$REPO" --body "$ARM_CLIENT_SECRET"
gh secret set ARM_TENANT_ID       --repo "$REPO" --body "$ARM_TENANT_ID"
gh secret set ARM_SUBSCRIPTION_ID --repo "$REPO" --body "$ARM_SUBSCRIPTION_ID"
ok "repo secrets set"

# --- 2. repo VARS: non-secret infra the Action reuses ---
log "Setting repo variables (HUB_* / TFSTATE_*)"
gh variable set HUB_ACR_NAME            --repo "$REPO" --body "$ACR_NAME"
gh variable set HUB_ACR_RG              --repo "$REPO" --body "$RG"
gh variable set HUB_LOCATION            --repo "$REPO" --body "$HUB_LOCATION"
gh variable set HUB_IMAGE_REF           --repo "$REPO" --body "$HUB_IMAGE_REF"
gh variable set TFSTATE_STORAGE_ACCOUNT --repo "$REPO" --body "$TFSTATE_SA"
gh variable set TFSTATE_CONTAINER       --repo "$REPO" --body "$TFSTATE_CT"
gh variable set HUB_KEYVAULT_NAME       --repo "$REPO" --body "$KV_NAME"
ok "repo variables set"

# --- 3. KV secret: GitHub PAT the control plane uses to dispatch + poll ---
log "Writing GitHub PAT to Key Vault secret hub-deploy-github-token"
az keyvault secret set --vault-name "$KV_NAME" --name "hub-deploy-github-token" \
  --value "$GITHUB_DEPLOY_PAT" --output none \
  || die "failed to write PAT to KV (need Key Vault Secrets Officer on $KV_NAME)"
ok "PAT stored in Key Vault"

# --- 4. RBAC safety-net: ensure the SP can read gh-<id>-jobinput at run time ---
# The full SP role bundle is create-deployer-sp.sh's job; this is a belt-and-
# suspenders re-grant of the ONE role this script's outputs depend on (the Action
# reads the per-account jobinput secret from KV). Idempotent — a no-op if
# create-deployer-sp.sh already granted it.
log "Ensuring the Service Principal has Key Vault Secrets User on $KV_NAME"
KV_ID="$(az keyvault show -n "$KV_NAME" --query id -o tsv)"
SP_OBJECT_ID="$(az ad sp show --id "$ARM_CLIENT_ID" --query id -o tsv 2>/dev/null || true)"
[[ -n "$SP_OBJECT_ID" ]] || die "could not resolve SP object id for appId $ARM_CLIENT_ID"
az role assignment create \
  --assignee-object-id "$SP_OBJECT_ID" \
  --assignee-principal-type ServicePrincipal \
  --role "Key Vault Secrets User" \
  --scope "$KV_ID" --output none 2>/dev/null \
  && ok "SP has Key Vault Secrets User" \
  || warn "role assignment may already exist (or insufficient perms) — verify manually"

log "Done. 方案 A is configured for $REPO against env $RG."
printf '  Next: push deploy-hub.yml to the repo, roll the control plane, and test a deploy.\n'
