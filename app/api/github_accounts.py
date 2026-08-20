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
  POST /github-accounts/{id}/relogin/start|poll -> re-authorize an EXISTING account
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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.auth import Principal, require_admin
from app.db import SessionLocal, get_db
from app.models.enums import (
    AuthMode,
    DeployStatus,
    OwnerScope,
    Provider,
)
from app.models.enums import (
    vendor_for_model as _vendor_for_model,
)
from app.models.orm import GitHubAccount, ModelRoute
from app.models.schemas import (
    DevicePollOut,
    DeviceStartOut,
    GitHubAccountOut,
    ReloginPollOut,
)
from app.services import copilot_device, terraform_runner
from app.services.apim_provisioner import ApimProvisioner
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)
router = APIRouter()

_HUB_MODELS_TIMEOUT = 30.0
_HUB_TOKEN_INSTALL_TIMEOUT = 30.0


def _provider_for_model(model_id: str) -> str | None:
    """Map a hub model id to its client-facing APIM provider API. Mirrors
    scripts/register_hub_models.py:
      claude-* -> anthropic (Messages API),
      gpt-*/o[0-9]-*/grok-*/kimi-* -> openai (Chat Completions + Responses),
      gemini-*  -> google (OpenAI-compatible).
    Anything else (embeddings, experimental, mai-*, trajectory-*) has no
    client-facing provider API, so it returns None and is skipped.

    NOTE this is the PROTOCOL, not the vendor — see `_vendor_for_model`. grok
    and kimi are xAI's and Moonshot's models served over the OpenAI-compatible
    schema, so they route through `llm-openai`; giving them their own provider
    would make the gateway build an `llm-xai` API and pool that no upstream
    endpoint answers.

    ⚠️ grok is served ONLY on /v1/responses. Measured 2026-08-20 through the
    gateway: /v1/chat/completions returns 400 `unsupported_api_for_model`
    ("not accessible via the /chat/completions endpoint") for grok-4.5 and
    grok-4.6, while /v1/responses answers 200 with a full `copilot_usage`. Both
    operations live on the same `llm-openai` API, so the route is correct — the
    caller just has to pick the right path.
    """
    m = model_id.lower()
    if m.startswith("claude"):
        return "anthropic"
    if m.startswith(("gpt", "o1-", "o3-", "o4-", "chatgpt", "grok", "kimi")):
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

    CONCURRENCY. `_deploy_account` runs this from a BackgroundTask, so adding
    several accounts at once runs several copies at once. The dedupe read must
    therefore sit as close to the insert as possible: an earlier version read
    the catalog at the top of the function and only then made the per-provider
    ARM calls, leaving a read-to-commit window tens of seconds wide. Three
    accounts added together on dev-16 each read an empty catalog and each
    inserted all 36 models — 108 rows for 36 distinct names, in three bursts
    26s/1.1s apart. The ARM calls now happen FIRST and the names are re-read
    immediately before inserting; a partial unique index in init_db is the
    backstop for the window that remains.
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

    # ARM work first, OUTSIDE the read-modify-write window. Wiring each
    # provider's API to its pool is idempotent and has nothing to do with the
    # route rows; doing it between the read and the insert is what made the
    # window wide enough to lose a race.
    provisioner = ApimProvisioner()
    pool_ids = {
        provider: provisioner.ensure_pooled_provider_api(provider)
        for provider in by_provider
    }

    all_routes = db.query(ModelRoute).all()
    # Scope the dedupe to PLATFORM routes. Matching on every route regardless of
    # scope meant a tenant's BYO route silently suppressed the platform one for
    # the same model name: the platform route was never created, so pooled
    # traffic for that model had nowhere to go, and nothing logged a reason.
    # The two are different objects with different backends and are allowed to
    # coexist — which is also why the unique index in init_db is partial.
    existing = {
        r.name
        for r in all_routes
        if r.owner_scope == OwnerScope.PLATFORM and r.tenant_id is None
    }
    created = 0
    for provider, models in by_provider.items():
        pool_id = pool_ids[provider]
        for mid in models:
            if mid in existing:
                continue
            db.add(
                ModelRoute(
                    id=f"rt_{uuid.uuid4().hex[:12]}",
                    tenant_id=None,  # platform-pooled (RESELL/INTERNAL)
                    name=mid,
                    provider=Provider(provider),
                    vendor=_vendor_for_model(mid),
                    apim_backend_or_pool_id=pool_id,
                    owner_scope=OwnerScope.PLATFORM,
                    auth_mode=AuthMode.MI,
                )
            )
            existing.add(mid)
            created += 1

    # Backfill `vendor` on rows that predate the column. Done here rather than as
    # a SQL default in init_db because the value is derived from the model NAME,
    # which SQL would have to re-implement — and because a resync is already the
    # operation an operator runs when the catalog looks wrong.
    backfilled = 0
    for r in all_routes:
        if r.vendor is None:
            v = _vendor_for_model(r.name)
            if v:
                r.vendor = v
                backfilled += 1

    removed = 0
    if prune:
        hub_model_ids = {mid for models in by_provider.values() for mid in models}
        for r in all_routes:
            # Only prune platform-pooled routes the hub no longer offers. Never
            # touch TENANT/BYO routes or anything the operator added manually.
            if r.owner_scope == OwnerScope.PLATFORM and r.name not in hub_model_ids:
                db.delete(r)
                removed += 1

    try:
        db.commit()
    except IntegrityError:
        # The partial unique index on (name) for PLATFORM routes fired: another
        # account's registration inserted the same model between our re-read and
        # this commit. That is the race working as intended — the row exists,
        # which is all we wanted — so roll back and carry on rather than failing
        # a deploy over a duplicate we did not need to create.
        db.rollback()
        logger.info(
            "hub catalog: concurrent registration won the race; routes already present"
        )
        return
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
# Re-login (recover an expired Copilot OAuth token without a redeploy)          #
# --------------------------------------------------------------------------- #
def _install_token_on_hub(fqdn: str, admin_token: str, oauth_token: str) -> None:
    """Hot-swap a running hub's Copilot OAuth token via its admin API.

    The hub validates the token against GitHub's exchange endpoint before
    keeping it, so a non-2xx here means the new token genuinely does not work —
    not that the call was malformed. Raises RuntimeError with the hub's own
    detail (rejected) or httpx.HTTPError (unreachable); the two need different
    advice, so they stay distinguishable.
    """
    with httpx.Client(timeout=_HUB_TOKEN_INSTALL_TIMEOUT) as hc:
        r = hc.post(
            f"https://{fqdn}/api/auth/copilot/token",
            headers={"x-admin-token": admin_token},
            json={"access_token": oauth_token},
        )
        if r.status_code >= 400:
            raise RuntimeError(f"hub rejected the token ({r.status_code}): {r.text[:400]}")


def _account_for_relogin(db: Session, account_id: str) -> GitHubAccount:
    """Fetch an account that can actually accept a new token, or 404/409."""
    acct = db.get(GitHubAccount, account_id)
    if not acct:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="account not found")
    # READY only: a pending account is mid-deploy and owns `device_code` for its
    # own flow, and a failed/deleting one has no hub to install the token into.
    if acct.status != DeployStatus.READY:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"account is {acct.status.value}; re-login needs a ready account",
        )
    if not acct.container_app_fqdn or not acct.admin_token_kv_ref:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="account has no hub endpoint / admin token (redeploy required)",
        )
    return acct


@router.post("/github-accounts/{account_id}/relogin/start", response_model=DeviceStartOut)
def relogin_start(
    account_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> DeviceStartOut:
    """Begin a device flow that REPLACES an existing account's Copilot token.

    Why this exists: the `ghu_` token minted by the device flow can stop being
    accepted without anything on our side changing — signing the same GitHub
    account in from somewhere else is the common trigger. The hub caches the
    exchanged API token in-process, so nothing looks wrong until that cache turns
    over, and then every request through this account's hub 503s. Before this
    endpoint the only fix was a full terraform redeploy (minutes, new revision)
    or a manual Key Vault + container edit; now it is one button and a browser
    round-trip.
    """
    acct = _account_for_relogin(db, account_id)
    flow = copilot_device.start_device_flow()
    acct.device_code = flow["device_code"]
    db.commit()
    return DeviceStartOut(
        account_id=account_id,
        user_code=flow["user_code"],
        verification_uri=flow["verification_uri"],
        interval=flow["interval"],
        expires_in=flow["expires_in"],
    )


@router.post("/github-accounts/{account_id}/relogin/poll", response_model=ReloginPollOut)
def relogin_poll(
    account_id: str,
    db: Session = Depends(get_db),
    _: Principal = Depends(require_admin),
) -> ReloginPollOut:
    """Poll the re-login flow; on success install the token and persist it.

    Order matters: the hub is updated FIRST and Key Vault only after that
    succeeds. Key Vault is the durable copy that a future redeploy injects, so
    it must never hold a token that was not proven to work — a hub that has been
    down for a week would otherwise be redeployed straight back into 401s.

    The deploy state machine is untouched throughout: the account was ready
    before and stays ready, because nothing is being deployed.
    """
    acct = _account_for_relogin(db, account_id)
    if not acct.device_code:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="no re-login in progress; call relogin/start first",
        )

    result = copilot_device.poll_device_flow(acct.device_code)
    if result["status"] == "pending":
        return ReloginPollOut(account_id=account_id, status="pending")
    if result["status"] == "error":
        acct.device_code = None
        db.commit()
        return ReloginPollOut(
            account_id=account_id,
            status="failed",
            detail=f"device flow: {result.get('error')}",
        )

    token = result["access_token"]
    who = copilot_device.whoami(token)

    # The browser that authorized may have been signed into a DIFFERENT GitHub
    # account. Installing that token would silently re-point this hub at someone
    # else's Copilot quota while the portal, the pool membership and every usage
    # record still say the original login — so refuse rather than guess.
    if acct.github_user_id and who.get("id") and who["id"] != acct.github_user_id:
        acct.device_code = None
        db.commit()
        return ReloginPollOut(
            account_id=account_id,
            status="failed",
            detail=(
                f"authorized as {who.get('login') or 'another user'}, "
                f"but this account is {acct.github_login or acct.github_user_id}"
            ),
        )

    kv = KeyVaultService()
    admin_token = kv.get_secret(acct.admin_token_kv_ref or "")
    if not admin_token:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="hub admin token not found in Key Vault",
        )

    try:
        _install_token_on_hub(acct.container_app_fqdn or "", admin_token, token)
    except (RuntimeError, httpx.HTTPError) as exc:
        acct.device_code = None
        db.commit()
        logger.warning("relogin: %s token install failed: %s", account_id, exc)
        return ReloginPollOut(account_id=account_id, status="failed", detail=str(exc)[:500])

    kv.set_secret(_github_token_name(account_id), token)
    acct.oauth_token_kv_ref = _github_token_name(account_id)
    acct.github_login = who.get("login") or acct.github_login
    acct.device_code = None
    db.commit()
    logger.info("relogin: %s token replaced (login=%s)", account_id, acct.github_login)
    return ReloginPollOut(
        account_id=account_id, status="success", github_login=acct.github_login
    )


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
    """Remove from pools, terraform destroy, clean KV + DB. Best-effort/idempotent.

    Ordering is load-bearing and the first TWO steps are GATES, not best effort:
    pool member -> per-account backend -> Azure resources -> KV + DB row.
    Either gate failing leaves every resource intact and the account marked
    FAILED, so pressing delete again is a clean retry. Only step 3 (KV + DB) is
    best-effort, because by then there is nothing left to strand.
    """
    db = SessionLocal()
    try:
        acct = db.get(GitHubAccount, account_id)
        if not acct:
            return
        # 1) remove from pools + delete per-account backends.
        #
        # This must SUCCEED before anything below runs. It used to be swallowed,
        # and the consequence was not a stale entry that self-heals: teardown
        # went on to destroy the hub and delete the account row, leaving a
        # backend still wired into a live pool but pointing at a Container App
        # that no longer existed — and no record left for a retry to work from.
        # Removing it took hand-editing ARM. Meanwhile a third of gateway
        # traffic round-robined into the dead hub.
        #
        # Failing here leaves EVERY resource intact and the account marked
        # FAILED, so pressing delete again is a clean retry.
        try:
            ApimProvisioner().remove_hub_from_pools(account_id, acct.backend_ids or [])
        except Exception as exc:  # noqa: BLE001
            logger.exception("_teardown_account: pool removal failed for %s", account_id)
            _fail(db, acct, f"APIM cleanup failed; nothing was destroyed — retry delete. {exc}")
            return
        # 2) terraform destroy the resource group.
        #
        # A GATE for the same reason step 1 is one. This used to be swallowed:
        # destroy raised, the log recorded it, and step 3 went on to delete the
        # KV secrets and the account row anyway. What that leaves is a Container
        # App plus a managed environment billing monthly with NO record left
        # anywhere to retry from — the operator has to find them by hand.
        #
        # Observed on dev-18 (2026-08-14): three accounts deleted from the UI,
        # all three destroy runs failed, all three rows removed 1 second later,
        # three resource groups orphaned. The destroys failed because the hub
        # workflow reads REPO-LEVEL Actions variables (KEYVAULT_NAME,
        # TFSTATE_STORAGE_ACCOUNT, ...) which are a single global slot that the
        # most recent `deploy.sh` overwrites — so dev-18's destroy ran against
        # dev-19's Key Vault and could not find its own job input. Any
        # environment that is not the most recently deployed one hits this, and
        # that is exactly when the failure must NOT be silent.
        token = None
        if acct.oauth_token_kv_ref:
            token = KeyVaultService().get_secret(acct.oauth_token_kv_ref)
        try:
            terraform_runner.destroy_hub(account_id, token or "")
        except Exception as exc:  # noqa: BLE001
            logger.exception("_teardown_account: destroy failed for %s", account_id)
            _fail(
                db,
                acct,
                "Azure resources were NOT destroyed and nothing else was "
                "removed — press delete again to retry. If this environment is "
                "not the most recently deployed one, the hub workflow is "
                "pointed at another environment's Key Vault and the retry will "
                f"fail the same way until that is corrected. {exc}",
            )
            return
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
