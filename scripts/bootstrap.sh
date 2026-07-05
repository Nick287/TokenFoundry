#!/usr/bin/env bash
#
# Token Foundry — one-command bootstrap: deploy the environment, then create the
# deployment Service Principal. Runs the two steps in order, stopping immediately
# if the first fails (so the SP is only created against a real, healthy env).
#
# This is a thin orchestrator — it does NOT reimplement anything. It calls:
#   1. scripts/deploy.sh              — provision the whole environment (KV, ACR,
#                                       APIM, tfstate storage, control plane) +
#                                       build images. Long (~30-75 min; APIM is
#                                       the long pole).
#   2. scripts/create-deployer-sp.sh  — create the 方案 A deployment SP, grant its
#                                       role bundle, and store its creds in the
#                                       env's Key Vault (deployer-sp-*).
#
# It deliberately STOPS before scripts/setup-github-deploy.sh: that step needs a
# GitHub PAT (GITHUB_DEPLOY_PAT) and `gh auth login`, which are interactive /
# out-of-band. Run it yourself afterwards — the tail of this script prints the
# exact command.
#
# Usage:
#   az login && az account set --subscription <id>
#   ./scripts/bootstrap.sh -g tokenfoundry-rg-dev-001
#   ./scripts/bootstrap.sh -g <rg> -t v2 --skip-build     # pass a tag / skip image build
#   ./scripts/bootstrap.sh -g <rg> --reset-password       # rotate the SP secret in KV
#   ./scripts/bootstrap.sh -g <rg> --no-uaa               # SP without User Access Administrator
#
# Options:
#   -g <rg>            (required) resource group of the target environment. Passed
#                      to create-deployer-sp.sh; deploy.sh derives its own RG from
#                      terraform config, so this is the SP/KV target.
#   -t <tag>           image tag for deploy.sh (default: deploy.sh's timestamp).
#   --skip-build       forward to deploy.sh (reuse existing image, terraform only).
#   --reset-password   forward to create-deployer-sp.sh (rotate SP secret).
#   --no-uaa           forward to create-deployer-sp.sh (skip User Access Admin).
#   -n <name>          forward to create-deployer-sp.sh (SP display name).
#   -h                 help.
#
# Prereqs: `az login` with rights to deploy + create SPs + assign roles. See the
# two sub-scripts' --help for their individual requirements.

set -euo pipefail

log()  { printf '\n\033[1;36m>>> %s\033[0m\n' "$*"; }
ok()   { printf '\033[1;32m%s\033[0m\n' "$*"; }
die()  { printf '\n\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,40p' "$0" | sed 's/^# \{0,1\}//'; exit "${1:-0}"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- args ---
RG=""
TAG=""
SP_NAME=""
SKIP_BUILD=false
RESET_PW=false
NO_UAA=false
while [[ $# -gt 0 ]]; do
  case "$1" in
    -g) RG="${2:-}"; shift 2 ;;
    -t) TAG="${2:-}"; shift 2 ;;
    -n) SP_NAME="${2:-}"; shift 2 ;;
    --skip-build) SKIP_BUILD=true; shift ;;
    --reset-password) RESET_PW=true; shift ;;
    --no-uaa) NO_UAA=true; shift ;;
    -h|--help) usage 0 ;;
    *) die "unknown argument: $1 (use -h for help)" ;;
  esac
done

[[ -n "$RG" ]] || die "resource group is required: -g <rg>"

# --- preflight: both sub-scripts must exist + be runnable ---
DEPLOY="$SCRIPT_DIR/deploy.sh"
CREATE_SP="$SCRIPT_DIR/create-deployer-sp.sh"
[[ -f "$DEPLOY"    ]] || die "missing $DEPLOY"
[[ -f "$CREATE_SP" ]] || die "missing $CREATE_SP"

# --- 1. deploy the environment ---
# deploy.sh's positional contract: $1=tag, $2=--skip-build. Only pass what's set
# so we don't clobber its timestamp default with an empty string.
log "STEP 1/2 — deploy.sh (provision environment + build images)"
DEPLOY_ARGS=()
[[ -n "$TAG" ]] && DEPLOY_ARGS+=("$TAG")
[[ "$SKIP_BUILD" == true ]] && DEPLOY_ARGS+=("--skip-build")
bash "$DEPLOY" "${DEPLOY_ARGS[@]}" || die "deploy.sh failed — SP creation skipped"
ok "STEP 1/2 complete — environment is deployed"

# --- 2. create the deployment SP (creds -> Key Vault) ---
log "STEP 2/2 — create-deployer-sp.sh (create SP, grant roles, store creds in KV)"
SP_ARGS=(-g "$RG")
[[ -n "$SP_NAME" ]] && SP_ARGS+=(-n "$SP_NAME")
[[ "$RESET_PW" == true ]] && SP_ARGS+=(--reset-password)
[[ "$NO_UAA"   == true ]] && SP_ARGS+=(--no-uaa)
bash "$CREATE_SP" "${SP_ARGS[@]}" || die "create-deployer-sp.sh failed"
ok "STEP 2/2 complete — SP is ready, creds stored in Key Vault"

# --- next step (left to you: needs GitHub PAT + gh auth) ---
log "Bootstrap done. Final step is manual (needs a GitHub PAT + gh login):"
printf '    export GITHUB_DEPLOY_PAT=github_pat_...\n'
printf '    ./scripts/setup-github-deploy.sh -g %s\n' "$RG"
