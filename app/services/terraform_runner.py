"""Per-account GitModel hub deployment — triggers a GitHub Action (方案 A).

Each GitHub account gets its OWN hub Container App in its OWN resource group. The
terraform that builds it runs in a **GitHub Action** (workflow `deploy-hub.yml`)
using Service Principal auth — the standard azurerm auth path that sidesteps the
ACA-Job "az login vs terraform MSI" deadlock. The control plane here only:

  1. generates the two hub secrets (admin_token, hub_api_key),
  2. writes a per-account job-input secret to Key Vault (the Action reads it),
  3. triggers the workflow (workflow_dispatch) with a correlation id,
  4. finds the run it triggered (dispatch doesn't reliably return a run id) and
     polls it to completion,
  5. reads terraform outputs (app_url, resource_group) from the remote-state blob,

and returns the SAME dict the earlier versions did — so callers
(app/api/github_accounts.py) are unchanged: {app_url, resource_group,
admin_token, hub_api_key}. Failures raise TerraformError.

Why GitHub Action: the SP creds live in GitHub repo secrets — the control plane
holds only a GitHub token (can trigger the predefined workflow, nothing else) and
blob-read on the state. A control-plane compromise can't reach the SP or alter
the terraform. Best isolation of the options tried (P1 in-process / P2 ACA Job).
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import secrets
import time

import httpx
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobClient

from app.config import get_settings
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
# How long to wait for the run to appear after dispatch, and to finish.
_FIND_RUN_TIMEOUT = 120     # find the triggered run within 2 min
_RUN_POLL_TIMEOUT = 1800    # run completes within 30 min
_POLL_INTERVAL = 10         # seconds


class TerraformError(RuntimeError):
    """A hub deploy/destroy run failed; message carries the reason."""


def _new_admin_token() -> str:
    """A deploy-time HUB_ADMIN_TOKEN the control plane injects so it can call the
    hub's management API without the portal login."""
    return secrets.token_urlsafe(32)


def _new_hub_key() -> str:
    """A deploy-time hub /v1 API key the control plane injects as HUB_API_KEY and
    ALSO hands to APIM as the per-account backend credential — both ends match
    with zero hub round-trip. Same `sk-hub-` shape the hub portal uses."""
    return f"sk-hub-{secrets.token_urlsafe(32)}"


def _jobinput_name(account_id: str) -> str:
    """KV secret name for the per-account job input the Action reads (JSON:
    oauth_token/admin_token/hub_api_key). The Action derives this same name from
    its account_id input."""
    return f"gh-{account_id.replace('_', '-')}-jobinput"


def fqdn_from_url(app_url: str) -> str:
    """app_url is https://<fqdn>; the pool backend wants the bare fqdn."""
    return app_url.replace("https://", "").replace("http://", "").rstrip("/")


# --------------------------------------------------------------------------- #
# GitHub REST helpers                                                         #
# --------------------------------------------------------------------------- #
def _github_token() -> str:
    s = get_settings()
    tok = KeyVaultService().get_secret(s.github_token_secret)
    if not tok:
        raise TerraformError(
            f"GitHub token not found in Key Vault (secret {s.github_token_secret})"
        )
    return tok


def _github_headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {_github_token()}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _workflow_base() -> str:
    s = get_settings()
    return (
        f"{_GITHUB_API}/repos/{s.github_repo_owner}/{s.github_repo_name}"
        f"/actions/workflows/{s.github_workflow_file}"
    )


def _write_jobinput(
    account_id: str, oauth_token: str, admin_token: str, hub_api_key: str
) -> None:
    """Write the per-account job input to Key Vault (the Action reads it on run).
    Secrets travel via KV, never as dispatch inputs / run logs."""
    payload = json.dumps({
        "account_id": account_id,
        "oauth_token": oauth_token,
        "admin_token": admin_token,
        "hub_api_key": hub_api_key,
    })
    KeyVaultService().set_secret(_jobinput_name(account_id), payload)


def _trigger_workflow(account_id: str, action: str, correlation_id: str) -> None:
    """POST workflow_dispatch. Returns nothing usable (204/no run id) — the
    caller finds the run via correlation_id + created-time filter."""
    s = get_settings()
    url = f"{_workflow_base()}/dispatches"
    body = {
        "ref": s.github_ref,
        "inputs": {
            "account_id": account_id,
            "action": action,
            "correlation_id": correlation_id,
        },
    }
    with httpx.Client(timeout=30.0) as hc:
        r = hc.post(url, headers=_github_headers(), json=body)
        if r.status_code not in (201, 204):
            raise TerraformError(
                f"workflow_dispatch failed ({r.status_code}): {r.text[:300]}"
            )
    logger.info(
        "dispatched %s for %s (corr=%s)",
        s.github_workflow_file, account_id, correlation_id,
    )


def _find_run(correlation_id: str, since_iso: str) -> int:
    """Find the run id of the run we just triggered. workflow_dispatch doesn't
    return a run id, so poll the workflow's runs filtered by created>=since and
    match run-name containing the correlation id."""
    url = f"{_workflow_base()}/runs"
    params = {"event": "workflow_dispatch", "created": f">={since_iso}", "per_page": 30}
    deadline = time.time() + _FIND_RUN_TIMEOUT
    while time.time() < deadline:
        with httpx.Client(timeout=30.0) as hc:
            r = hc.get(url, headers=_github_headers(), params=params)
            r.raise_for_status()
            for run in r.json().get("workflow_runs", []):
                name = run.get("name", "") or run.get("display_title", "")
                if correlation_id in name:
                    logger.info("matched run %s for corr=%s", run["id"], correlation_id)
                    return int(run["id"])
        time.sleep(5)
    raise TerraformError(
        f"could not find the triggered run for corr={correlation_id} within "
        f"{_FIND_RUN_TIMEOUT}s"
    )


def _poll_run(run_id: int, account_id: str) -> None:
    """Poll a run to completion. Raises TerraformError unless conclusion=success."""
    s = get_settings()
    url = (
        f"{_GITHUB_API}/repos/{s.github_repo_owner}/{s.github_repo_name}"
        f"/actions/runs/{run_id}"
    )
    deadline = time.time() + _RUN_POLL_TIMEOUT
    while time.time() < deadline:
        with httpx.Client(timeout=30.0) as hc:
            r = hc.get(url, headers=_github_headers())
            r.raise_for_status()
            data = r.json()
        status = data.get("status", "")
        conclusion = data.get("conclusion", "")
        logger.info("run %s (%s) status=%s conclusion=%s", run_id, account_id, status, conclusion)
        if status == "completed":
            if conclusion == "success":
                return
            raise TerraformError(
                f"deploy run {run_id} for {account_id} ended {conclusion} — see "
                f"{data.get('html_url', 'GitHub Actions')}"
            )
        time.sleep(_POLL_INTERVAL)
    raise TerraformError(
        f"deploy run {run_id} for {account_id} did not finish within {_RUN_POLL_TIMEOUT}s"
    )


def _read_state_outputs(account_id: str) -> dict[str, str]:
    """Download the per-account remote-state blob and parse terraform outputs
    (app_url, resource_group). No terraform needed — the control plane reads the
    state JSON directly via its managed identity (Storage Blob Data Reader)."""
    s = get_settings()
    blob_url = (
        f"https://{s.tfstate_storage_account}.blob.core.windows.net"
        f"/{s.tfstate_container}/hubs/{account_id}.tfstate"
    )
    client = BlobClient.from_blob_url(blob_url, credential=DefaultAzureCredential())
    raw = client.download_blob().readall()
    state = json.loads(raw)
    outputs = state.get("outputs", {})
    app_url = (outputs.get("app_url") or {}).get("value", "")
    rg = (outputs.get("resource_group") or {}).get("value", "")
    if not app_url:
        raise TerraformError(
            f"state blob for {account_id} has no app_url output (deploy may have failed)"
        )
    return {"app_url": app_url, "resource_group": rg}


# --------------------------------------------------------------------------- #
# Public surface (unchanged contract)                                         #
# --------------------------------------------------------------------------- #
def deploy_hub(account_id: str, oauth_token: str) -> dict[str, str]:
    """Deploy one account's hub via the GitHub Action. Returns {app_url,
    resource_group, admin_token, hub_api_key}. Blocking (minutes) — call from a
    background task. admin_token/hub_api_key are generated here and injected into
    the hub by the Action; app_url/resource_group come back from remote state."""
    admin_token = _new_admin_token()
    hub_api_key = _new_hub_key()
    _write_jobinput(account_id, oauth_token, admin_token, hub_api_key)

    since = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    correlation_id = f"{account_id}-{secrets.token_hex(4)}"
    _trigger_workflow(account_id, "apply", correlation_id)
    run_id = _find_run(correlation_id, since)
    _poll_run(run_id, account_id)

    outputs = _read_state_outputs(account_id)
    return {
        "app_url": outputs["app_url"],
        "resource_group": outputs["resource_group"],
        "admin_token": admin_token,
        "hub_api_key": hub_api_key,
    }


def destroy_hub(account_id: str, oauth_token: str = "") -> None:
    """Destroy one account's hub via the GitHub Action (action=destroy).
    Idempotent: terraform destroy against remote state no-ops if already gone."""
    # The destroy still needs the jobinput secret present (the Action reads it to
    # set TF_VAR_* even for destroy); write it with whatever we have.
    _write_jobinput(account_id, oauth_token or "", "x", "x")
    since = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    correlation_id = f"{account_id}-destroy-{secrets.token_hex(4)}"
    _trigger_workflow(account_id, "destroy", correlation_id)
    run_id = _find_run(correlation_id, since)
    _poll_run(run_id, account_id)
