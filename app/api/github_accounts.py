"""GitHub-account-backed hub onboarding (GitModel fusion).

"Adding a model" becomes "adding a GitHub account": the user runs a GitHub
device-flow login, and the control plane then deploys a dedicated GitModel hub
(one Container App in its own resource group, backed by that account's Copilot
subscription) and registers it into the openai/anthropic/google APIM pools with
session affinity — so multiple accounts load-balance while prompt caching stays
warm.

Endpoints (admin-only, mirrors app/api/routes.py):
  POST /github-accounts/device/start  -> begin device flow, create a pending record
  POST /github-accounts/device/poll   -> poll GitHub; on success kick off deploy
  GET  /github-accounts               -> list accounts + their deploy status
  DELETE /github-accounts/{id}        -> destroy the hub + remove from pools

Deploy/teardown are slow (terraform, minutes) so they run as FastAPI background
tasks; the DB row is a DeployStatus state machine the frontend polls. This is the
P1 shape (in-process background task); P2 moves it to an ACA Job with remote
state and a dedicated deployer identity (see the plan).
"""

from __future__ import annotations

import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import SessionLocal, get_db
from app.models.enums import DeployStatus
from app.models.orm import GitHubAccount
from app.models.schemas import DevicePollOut, DeviceStartOut, GitHubAccountOut
from app.services import copilot_device, terraform_runner
from app.services.apim_provisioner import ApimProvisioner
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)
router = APIRouter()


def _github_token_name(account_id: str) -> str:
    """Key Vault secret name holding an account's Copilot OAuth token.

    Key Vault secret names allow only alphanumerics and dashes, so the account
    id's underscores (gha_xxx) are replaced with dashes.
    """
    return f"gh-{account_id.replace('_', '-')}-oauth"


def _hub_key_name(account_id: str) -> str:
    """Key Vault secret name for an account's hub /v1 API key (HUB_API_KEY)."""
    return f"gh-{account_id.replace('_', '-')}-hubkey"


def _admin_token_name(account_id: str) -> str:
    """Key Vault secret name for an account's hub admin token (HUB_ADMIN_TOKEN)."""
    return f"gh-{account_id.replace('_', '-')}-admin"


@router.post("/github-accounts/device/start", response_model=DeviceStartOut)
def device_start(
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> DeviceStartOut:
    """Begin GitHub device flow and create a pending account record."""
    flow = copilot_device.start_device_flow()
    account_id = f"gha_{uuid.uuid4().hex[:12]}"
    acct = GitHubAccount(
        id=account_id,
        status=DeployStatus.PENDING,
        device_code=flow["device_code"],
    )
    db.add(acct)
    db.commit()
    return DeviceStartOut(
        account_id=account_id,
        user_code=flow["user_code"],
        verification_uri=flow["verification_uri"],
        interval=flow["interval"],
        expires_in=flow["expires_in"],
    )


@router.post("/github-accounts/device/poll", response_model=DevicePollOut)
def device_poll(
    account_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> DevicePollOut:
    """Poll GitHub once. On first success: store the token in KV, flip to
    deploying, and kick off the background deploy. Idempotent for later polls
    (returns the current status once past pending)."""
    acct = db.get(GitHubAccount, account_id)
    if not acct:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")

    # Already moving/finished — just report current state (frontend keeps polling).
    if acct.status != DeployStatus.PENDING:
        return DevicePollOut(
            account_id=account_id, status=acct.status,
            github_login=acct.github_login, detail=acct.error_detail,
        )

    result = copilot_device.poll_device_flow(acct.device_code or "")
    if result["status"] == "pending":
        return DevicePollOut(account_id=account_id, status=DeployStatus.PENDING)
    if result["status"] == "error":
        acct.status = DeployStatus.FAILED
        acct.error_detail = f"device flow: {result.get('error')}"
        db.commit()
        return DevicePollOut(
            account_id=account_id, status=DeployStatus.FAILED, detail=acct.error_detail
        )

    # success: persist token to KV, label the account, hand off to background deploy.
    token = result["access_token"]
    who = copilot_device.whoami(token)
    kv = KeyVaultService()
    kv.set_secret(_github_token_name(account_id), token)
    acct.oauth_token_kv_ref = _github_token_name(account_id)
    acct.github_login = who.get("login")
    acct.github_user_id = who.get("id")
    acct.device_code = None
    acct.status = DeployStatus.DEPLOYING
    db.commit()

    background.add_task(_deploy_account, account_id)
    return DevicePollOut(
        account_id=account_id, status=DeployStatus.DEPLOYING, github_login=acct.github_login
    )


@router.get("/github-accounts", response_model=list[GitHubAccountOut])
def list_accounts(
    db: Session = Depends(get_db), _: Principal = Depends(require_admin)
) -> list[GitHubAccount]:
    return list(db.query(GitHubAccount).all())


@router.delete("/github-accounts/{account_id}", status_code=status.HTTP_202_ACCEPTED)
def delete_account(
    account_id: str,
    background: BackgroundTasks,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> dict[str, str]:
    """Flip to deleting and tear down in the background (terraform destroy +
    remove from pools + clean KV/DB)."""
    acct = db.get(GitHubAccount, account_id)
    if not acct:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    acct.status = DeployStatus.DELETING
    db.commit()
    background.add_task(_teardown_account, account_id)
    return {"account_id": account_id, "status": DeployStatus.DELETING.value}


# --------------------------------------------------------------------------- #
# Background orchestration (own DB session — not the request's)                #
# --------------------------------------------------------------------------- #
def _deploy_account(account_id: str) -> None:
    """Deploy the hub for an authorized account and join it to the pools.
    Runs in the background; drives the DeployStatus state machine."""
    db = SessionLocal()
    try:
        acct = db.get(GitHubAccount, account_id)
        if not acct or not acct.oauth_token_kv_ref:
            logger.error("_deploy_account: %s missing or no token", account_id)
            return
        token = KeyVaultService().get_secret(acct.oauth_token_kv_ref)
        if not token:
            _fail(db, acct, "oauth token not found in Key Vault")
            return

        # 1) deploy the hub via the ACA Job (P2). deploy_hub generates and
        #    injects HUB_ADMIN_TOKEN + HUB_API_KEY into the (stateless) hub and
        #    returns both, so we never round-trip the hub to mint a key — the
        #    hub_api_key we hold IS the inbound credential the hub accepts.
        deployed = terraform_runner.deploy_hub(account_id, token)
        fqdn = terraform_runner.fqdn_from_url(deployed["app_url"])
        hub_api_key = deployed["hub_api_key"]
        admin_token = deployed["admin_token"]
        acct.container_app_fqdn = fqdn
        acct.resource_group = deployed["resource_group"]
        # Record the remote-state key so teardown / future reconcilers can locate
        # this account's terraform state without a local workdir.
        acct.tf_state_key = f"hubs/{account_id}.tfstate"

        # 2) persist the injected secrets in Key Vault (DB keeps only references).
        kv = KeyVaultService()
        kv.set_secret(_hub_key_name(account_id), hub_api_key)
        kv.set_secret(_admin_token_name(account_id), admin_token)
        acct.hub_key_kv_ref = _hub_key_name(account_id)
        acct.admin_token_kv_ref = _admin_token_name(account_id)
        db.commit()

        # 3) register the hub into the 3 provider pools (session affinity kept),
        #    using the injected hub key as the APIM backend credential — both
        #    ends match, zero hub round-trip, no revision-rollout race.
        backend_ids = ApimProvisioner().add_hub_to_pools(account_id, fqdn, hub_api_key)
        acct.backend_ids = backend_ids
        acct.status = DeployStatus.READY
        acct.error_detail = None
        db.commit()
        logger.info("_deploy_account: %s ready (fqdn=%s)", account_id, fqdn)
    except Exception as exc:  # noqa: BLE001 — record the failure, don't crash the worker
        logger.exception("_deploy_account: %s failed", account_id)
        acct = db.get(GitHubAccount, account_id)
        if acct:
            _fail(db, acct, str(exc)[:2000])
    finally:
        db.close()


def _teardown_account(account_id: str) -> None:
    """Remove from pools, terraform destroy, clean KV + DB. Best-effort/idempotent."""
    db = SessionLocal()
    try:
        acct = db.get(GitHubAccount, account_id)
        if not acct:
            return
        # 1) remove from pools + delete per-account backends
        try:
            ApimProvisioner().remove_hub_from_pools(account_id, acct.backend_ids or [])
        except Exception:  # noqa: BLE001
            logger.exception("_teardown_account: pool removal failed for %s", account_id)
        # 2) terraform destroy the resource group
        token = None
        if acct.oauth_token_kv_ref:
            token = KeyVaultService().get_secret(acct.oauth_token_kv_ref)
        try:
            terraform_runner.destroy_hub(account_id, token or "")
        except Exception:  # noqa: BLE001
            logger.exception("_teardown_account: destroy failed for %s", account_id)
        # 3) clean KV secrets (oauth + hub key + admin token + job in/out) + DB row
        kv = KeyVaultService()
        _dash = account_id.replace("_", "-")
        for ref in (
            acct.oauth_token_kv_ref,
            acct.hub_key_kv_ref,
            acct.admin_token_kv_ref,
            f"gh-{_dash}-jobinput",
            f"gh-{_dash}-outputs",
        ):
            if not ref:
                continue
            try:
                kv.delete_secret(ref)
            except Exception:  # noqa: BLE001
                logger.info(
                    "_teardown_account: KV secret cleanup skipped for %s (%s)",
                    account_id, ref,
                )
        db.delete(acct)
        db.commit()
        logger.info("_teardown_account: %s removed", account_id)
    finally:
        db.close()


def _fail(db: Session, acct: GitHubAccount, detail: str) -> None:
    acct.status = DeployStatus.FAILED
    acct.error_detail = detail
    db.commit()
