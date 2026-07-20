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

Deploy/teardown are slow (minutes) so they run as FastAPI background tasks; the
DB row is a DeployStatus state machine the frontend polls. The actual hub
terraform runs in a GitHub Action (方案 A) — the background task here triggers
that Action, polls the run, and reads the resulting outputs from remote state
(see app/services/terraform_runner.py).
"""

from __future__ import annotations

import logging
import uuid

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import SessionLocal, get_db
from app.models.enums import AuthMode, DeployStatus, OwnerScope, Provider
from app.models.orm import GitHubAccount, ModelRoute
from app.models.schemas import DevicePollOut, DeviceStartOut, GitHubAccountOut
from app.services import copilot_device, terraform_runner
from app.services.apim_provisioner import ApimProvisioner
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)
router = APIRouter()

_HUB_MODELS_TIMEOUT = 30.0


def _provider_for_model(model_id: str) -> str | None:
    """Map a hub model id to its client-facing APIM provider API. Mirrors
    scripts/register_hub_models.py:
      claude-* -> anthropic (Messages API),
      gpt-*/o[0-9]-* -> openai (Chat Completions + Responses),
      gemini-*  -> google (OpenAI-compatible).
    Anything else (embeddings, experimental, mai-*, trajectory-*) has no
    client-facing provider API, so it returns None and is skipped."""
    m = model_id.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1-", "o3-", "o4-", "chatgpt")):
        return "openai"
    if m.startswith("gemini"):
        return "google"
    return None


def _fetch_hub_models(fqdn: str, admin_token: str) -> list[str]:
    """Fetch the hub's chat-model catalog via its admin API (`GET /api/models`).
    Returns model ids where type == 'chat'. Raises on transport/HTTP error so
    the caller can log and continue (catalog registration is non-fatal)."""
    url = f"https://{fqdn}/api/models"
    with httpx.Client(timeout=_HUB_MODELS_TIMEOUT) as hc:
        r = hc.get(url, headers={"x-admin-token": admin_token})
        r.raise_for_status()
        payload = r.json()
    rows = payload.get("data", []) if isinstance(payload, dict) else payload
    return [
        m["id"]
        for m in rows
        if isinstance(m, dict) and m.get("id") and m.get("type") == "chat"
    ]


def _register_hub_catalog(
    db: Session, fqdn: str, admin_token: str, *, prune: bool = False
) -> None:
    """Discover the hub's chat models and register the not-yet-known ones as
    platform-pooled model routes, wiring each provider's client-facing APIM API
    to its LOAD-BALANCED POOL (`llm-<provider>-pool`) so requests fan out across
    every account's hub with session affinity (prompt-cache warmth — see
    docs/APIM-LLM-Gateway.md §2/§4).

    Idempotent: routes already present (by name) are skipped and
    ensure_pooled_provider_api is a no-op update, so running this on every
    account deploy is safe — the first account seeds the catalog, later accounts
    only add pool members.

    prune=True additionally DELETES platform-pooled (owner_scope=PLATFORM) routes
    whose model id is no longer in the hub's catalog — a true two-way sync that
    drops retired models. Off by default because the deploy path is multi-account
    (another account's hub may still serve a model this one dropped); only the
    manual resync action, which the operator invokes deliberately, prunes. TENANT
    (BYO) routes are never touched.
    """
    model_ids = _fetch_hub_models(fqdn, admin_token)
    by_provider: dict[str, list[str]] = {}
    for mid in model_ids:
        prov = _provider_for_model(mid)
        if prov:
            by_provider.setdefault(prov, []).append(mid)
    if not by_provider:
        logger.warning("hub catalog empty/unmappable; no model routes registered")
        return

    all_routes = db.query(ModelRoute).all()
    existing = {r.name for r in all_routes}
    provisioner = ApimProvisioner()
    created = 0
    for provider, models in by_provider.items():
        # Wire the provider's API -> its pool once (idempotent). This is what
        # makes the APIs appear under APIM > APIs.
        pool_id = provisioner.ensure_pooled_provider_api(provider)
        for mid in models:
            if mid in existing:
                continue
            db.add(
                ModelRoute(
                    id=f"rt_{uuid.uuid4().hex[:12]}",
                    tenant_id=None,  # platform-pooled (RESELL/INTERNAL)
                    name=mid,
                    provider=Provider(provider),
                    apim_backend_or_pool_id=pool_id,
                    owner_scope=OwnerScope.PLATFORM,
                    auth_mode=AuthMode.MI,
                )
            )
            existing.add(mid)
            created += 1

    removed = 0
    if prune:
        hub_model_ids = {mid for models in by_provider.values() for mid in models}
        for r in all_routes:
            # Only prune platform-pooled routes the hub no longer offers. Never
            # touch TENANT/BYO routes or anything the operator added manually.
            if r.owner_scope == OwnerScope.PLATFORM and r.name not in hub_model_ids:
                db.delete(r)
                removed += 1

    db.commit()
    logger.info(
        "hub catalog: +%d new / -%d pruned model routes across %d providers (%s)",
        created,
        removed,
        len(by_provider),
        ", ".join(sorted(by_provider)),
    )


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


@router.post("/github-accounts/{account_id}/resync-catalog")
def resync_catalog(
    account_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> dict[str, object]:
    """Re-run hub model-catalog registration for an already-deployed account.

    Catalog registration during deploy is best-effort and can fail if the hub
    isn't serving yet when `_deploy_account` reaches it (a slow hub cold-start
    leaves the account READY but with zero model routes). This admin action
    retries it against the now-live hub. Idempotent — known models are skipped.
    Runs synchronously (a catalog fetch + a few APIM PUTs, a handful of seconds).
    """
    acct = db.get(GitHubAccount, account_id)
    if not acct:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    if not acct.container_app_fqdn:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="account has no hub endpoint yet (not deployed / still deploying)",
        )
    if not acct.admin_token_kv_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="account has no admin token reference (redeploy required)",
        )
    admin_token = KeyVaultService().get_secret(acct.admin_token_kv_ref)
    if not admin_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="hub admin token not found in Key Vault",
        )
    before = db.query(ModelRoute).count()
    try:
        _register_hub_catalog(db, acct.container_app_fqdn, admin_token, prune=True)
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"hub catalog fetch failed: {exc}",
        ) from exc
    after = db.query(ModelRoute).count()
    return {"account_id": account_id, "routes_before": before, "routes_after": after}


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

        # 1) deploy the hub via the GitHub Action (方案 A). deploy_hub generates
        #    and injects HUB_ADMIN_TOKEN + HUB_API_KEY into the (stateless) hub and
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

        # 4) discover the hub's model catalog and register any new models as
        #    platform-pooled routes, wiring each provider's client-facing API to
        #    its POOL (so the APIs actually appear + fan out with affinity). The
        #    first account seeds the catalog; later accounts just add pool members
        #    (idempotent, skips already-known models). Non-fatal: a catalog hiccup
        #    must not fail an otherwise-healthy deploy — the account is READY once
        #    it's in the pools; models can be (re)synced later.
        try:
            _register_hub_catalog(db, fqdn, admin_token)
        except Exception:  # noqa: BLE001 — catalog is best-effort, don't fail deploy
            logger.exception("_deploy_account: %s catalog registration failed", account_id)

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
