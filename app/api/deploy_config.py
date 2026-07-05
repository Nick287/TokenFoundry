"""GitHub deploy configuration (方案 A onboarding, Portal-driven).

The GitHub wiring for cloud hub deploys, done in the Portal instead of a shell
script. An admin pastes two GitHub PATs; the control plane stores them in Key
Vault and pushes the Service Principal creds (already in KV as deployer-sp-* from
create-deployer-sp.sh) into the repo's Actions secrets/variables so the
deploy-hub.yml workflow can authenticate + run. Only once that push succeeds does
the 'Add GitHub account' flow unlock (the readiness gate).

Why two PATs:
  * GITHUB_BOOTSTRAP_PAT (KV: github-bootstrap-pat) — repo Administration/Secrets
    write; used HERE to push SP creds as repo secrets. High-privilege, one-off.
  * GITHUB_DEPLOY_PAT (KV: hub-deploy-github-token — the SAME name
    terraform_runner.py already reads) — Actions RW; used at runtime by the
    control plane to dispatch + poll the workflow.

GitHub can't mint PATs via API (by design), so the human pastes them — this just
stores + wires them. Endpoints are admin-only (mirrors github_accounts.py).
"""

from __future__ import annotations

import logging

from azure.core.exceptions import ResourceNotFoundError
from fastapi import APIRouter, Depends, HTTPException, status

from app.api.auth import Principal, require_admin
from app.config import get_settings
from app.models.schemas import DeployConfigStatus, DeployPatsIn
from app.services.github_repo import GitHubRepoConfigurator, GitHubRepoError
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)
router = APIRouter()

# KV secret names.
_BOOTSTRAP_PAT_SECRET = "github-bootstrap-pat"
_CONFIGURED_FLAG_SECRET = "github-repo-configured"  # "true" once the push succeeds
# SP creds written by scripts/create-deployer-sp.sh -> mapped to repo secrets.
_SP_SECRET_MAP = {
    "deployer-sp-client-id": "ARM_CLIENT_ID",
    "deployer-sp-client-secret": "ARM_CLIENT_SECRET",
    "deployer-sp-tenant-id": "ARM_TENANT_ID",
    "deployer-sp-subscription-id": "ARM_SUBSCRIPTION_ID",
}


def _kv() -> KeyVaultService:
    return KeyVaultService()


def _get_or_none(kv: KeyVaultService, name: str) -> str | None:
    """Read a KV secret, treating 'not found' as None (not an error)."""
    try:
        return kv.get_secret(name)
    except ResourceNotFoundError:
        return None


def _deploy_pat_secret_name() -> str:
    """The deploy PAT reuses the existing KV name terraform_runner.py reads."""
    return get_settings().github_token_secret


def _compute_status(kv: KeyVaultService, detail: str | None = None) -> DeployConfigStatus:
    """Derive readiness from KV state. ready = deploy PAT present AND the repo
    has been configured (SP creds pushed)."""
    bootstrap_set = _get_or_none(kv, _BOOTSTRAP_PAT_SECRET) is not None
    deploy_set = _get_or_none(kv, _deploy_pat_secret_name()) is not None
    sp_present = all(_get_or_none(kv, n) for n in _SP_SECRET_MAP)
    pushed = _get_or_none(kv, _CONFIGURED_FLAG_SECRET) == "true"
    return DeployConfigStatus(
        bootstrap_pat_set=bootstrap_set,
        deploy_pat_set=deploy_set,
        sp_creds_present=sp_present,
        pushed=pushed,
        ready=deploy_set and pushed,
        detail=detail,
    )


def _repo_variables() -> dict[str, str]:
    """The HUB_* / TFSTATE_* Actions variables the workflow reads. Every value is
    injected by terraform (see terraform/modules/containerapps) — the app does no
    string parsing or az query."""
    s = get_settings()
    return {
        "HUB_ACR_NAME": s.acr_name,
        "HUB_ACR_RG": s.resource_group,
        "HUB_LOCATION": s.azure_location,
        "HUB_IMAGE_REF": "gitmodel:latest",
        "TFSTATE_STORAGE_ACCOUNT": s.tfstate_storage_account,
        "TFSTATE_CONTAINER": s.tfstate_container,
        "HUB_KEYVAULT_NAME": s.keyvault_name,
    }


def _push_sp_to_github(kv: KeyVaultService) -> None:
    """Push SP creds as repo secrets + infra as repo variables, authenticated
    with the bootstrap PAT. Raises HTTPException(409) if prerequisites are
    missing, or 502 on a GitHub failure (message surfaced so the Portal can show
    what went wrong)."""
    s = get_settings()
    bootstrap = _get_or_none(kv, _BOOTSTRAP_PAT_SECRET)
    if not bootstrap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="bootstrap PAT not set — paste GITHUB_BOOTSTRAP_PAT first",
        )
    secrets_out: dict[str, str] = {}
    for kv_name, gh_name in _SP_SECRET_MAP.items():
        val = _get_or_none(kv, kv_name)
        if not val:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"SP cred {kv_name} missing in Key Vault — run create-deployer-sp.sh first",
            )
        secrets_out[gh_name] = val

    configurator = GitHubRepoConfigurator(
        owner=s.github_repo_owner, repo=s.github_repo_name, token=bootstrap
    )
    try:
        configurator.push(secrets_out, _repo_variables())
    except GitHubRepoError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc)
        ) from exc
    # Mark the repo configured so the gate can open.
    kv.set_secret(_CONFIGURED_FLAG_SECRET, "true")
    logger.info(
        "pushed SP creds + vars to %s/%s", s.github_repo_owner, s.github_repo_name
    )


@router.get("/deploy-config/status", response_model=DeployConfigStatus)
def get_status(_: Principal = Depends(require_admin)) -> DeployConfigStatus:
    """Report readiness of the GitHub deploy wiring (drives the add-account gate)."""
    return _compute_status(_kv())


@router.post("/deploy-config/pats", response_model=DeployConfigStatus)
def save_pats(
    body: DeployPatsIn, _: Principal = Depends(require_admin)
) -> DeployConfigStatus:
    """Store either/both PATs in Key Vault, then (best-effort) auto-push the SP
    creds to GitHub so the repo is configured in one step. A push failure is
    reported in `detail` but does not lose the stored PATs."""
    kv = _kv()
    if body.bootstrap_pat:
        kv.set_secret(_BOOTSTRAP_PAT_SECRET, body.bootstrap_pat)
    if body.deploy_pat:
        kv.set_secret(_deploy_pat_secret_name(), body.deploy_pat)

    # Auto-push when we have what we need (bootstrap PAT + SP creds).
    detail: str | None = None
    bootstrap = _get_or_none(kv, _BOOTSTRAP_PAT_SECRET)
    sp_ready = all(_get_or_none(kv, n) for n in _SP_SECRET_MAP)
    if bootstrap and sp_ready:
        try:
            _push_sp_to_github(kv)
        except HTTPException as exc:
            detail = str(exc.detail)
            logger.warning("auto-push after saving PATs failed: %s", detail)
    elif not sp_ready:
        detail = "PATs saved. SP creds not in Key Vault yet — run create-deployer-sp.sh, then Push."
    return _compute_status(kv, detail=detail)


@router.post("/deploy-config/push-sp", response_model=DeployConfigStatus)
def push_sp(_: Principal = Depends(require_admin)) -> DeployConfigStatus:
    """Manually (re)push the SP creds to GitHub — use after rotating the SP."""
    kv = _kv()
    _push_sp_to_github(kv)
    return _compute_status(kv)
