"""GitHub repo Actions secrets/variables via REST (no `gh` CLI).

The Portal's "push SP creds to GitHub" flow (app/api/deploy_config.py) uses this
to write the Service Principal creds as repo ACTIONS SECRETS (ARM_*) and the
infra config as repo ACTIONS VARIABLES (HUB_* / TFSTATE_*), so the deploy-hub.yml
workflow can authenticate + run.

Secrets must be encrypted client-side with the repo's libsodium public key
(sealed box) before upload — that's the only reason we depend on PyNaCl. The
encrypt step is a pure function (`encrypt_secret`) so it's unit-testable without
network. Everything else is thin httpx around the documented endpoints:

  GET  /repos/{o}/{r}/actions/secrets/public-key      -> {key_id, key}
  PUT  /repos/{o}/{r}/actions/secrets/{name}          -> {encrypted_value, key_id}
  GET  /repos/{o}/{r}/actions/variables/{name}        -> 200 if exists / 404
  POST /repos/{o}/{r}/actions/variables               -> create {name, value}
  PATCH/repos/{o}/{r}/actions/variables/{name}        -> update {name, value}

Auth uses the caller-supplied token (the GITHUB_BOOTSTRAP_PAT, which needs repo
Administration / Secrets write) — NOT the long-lived deploy token.
"""

from __future__ import annotations

import base64
import logging

import httpx
from nacl import public

logger = logging.getLogger(__name__)

_GITHUB_API = "https://api.github.com"
_HTTP_TIMEOUT = 30.0


class GitHubRepoError(RuntimeError):
    """A GitHub repo secrets/variables call failed; message carries the reason."""


def encrypt_secret(public_key_b64: str, secret_value: str) -> str:
    """Encrypt a secret with a repo's base64 libsodium public key (sealed box).

    Returns the base64 ciphertext GitHub's `PUT secrets` endpoint expects. Pure
    function — no network — so it can be unit-tested with a known keypair. We
    base64-decode the key ourselves and hand PyNaCl raw 32 bytes (avoids the
    Base64Encoder type dance and keeps the types clean)."""
    pub = public.PublicKey(base64.b64decode(public_key_b64))
    sealed = public.SealedBox(pub)
    ciphertext = sealed.encrypt(secret_value.encode("utf-8"))
    return base64.b64encode(ciphertext).decode("utf-8")


class GitHubRepoConfigurator:
    """Sets repo Actions secrets + variables for one (owner, repo), authenticated
    with a PAT. One instance per push; reuses a single public-key fetch."""

    def __init__(self, owner: str, repo: str, token: str) -> None:
        self._owner = owner
        self._repo = repo
        self._token = token
        self._base = f"{_GITHUB_API}/repos/{owner}/{repo}"
        self._pubkey: tuple[str, str] | None = None  # (key_id, key_b64)

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def _get_public_key(self, hc: httpx.Client) -> tuple[str, str]:
        """Fetch (and cache) the repo's Actions secrets public key."""
        if self._pubkey is not None:
            return self._pubkey
        r = hc.get(f"{self._base}/actions/secrets/public-key", headers=self._headers())
        if r.status_code != 200:
            raise GitHubRepoError(
                f"could not read repo public key ({r.status_code}): {r.text[:300]}"
            )
        data = r.json()
        self._pubkey = (data["key_id"], data["key"])
        return self._pubkey

    def set_secret(self, hc: httpx.Client, name: str, value: str) -> None:
        """Create/update one encrypted Actions secret (idempotent PUT)."""
        key_id, key_b64 = self._get_public_key(hc)
        body = {"encrypted_value": encrypt_secret(key_b64, value), "key_id": key_id}
        r = hc.put(
            f"{self._base}/actions/secrets/{name}", headers=self._headers(), json=body
        )
        if r.status_code not in (201, 204):
            raise GitHubRepoError(
                f"failed to set secret {name} ({r.status_code}): {r.text[:300]}"
            )
        logger.info("set repo secret %s on %s/%s", name, self._owner, self._repo)

    def set_variable(self, hc: httpx.Client, name: str, value: str) -> None:
        """Create/update one plaintext Actions variable. GitHub has no upsert:
        POST creates, PATCH updates — so probe with GET first."""
        exists = hc.get(
            f"{self._base}/actions/variables/{name}", headers=self._headers()
        )
        if exists.status_code == 200:
            r = hc.patch(
                f"{self._base}/actions/variables/{name}",
                headers=self._headers(),
                json={"name": name, "value": value},
            )
            ok_codes = (204,)
        else:
            r = hc.post(
                f"{self._base}/actions/variables",
                headers=self._headers(),
                json={"name": name, "value": value},
            )
            ok_codes = (201,)
        if r.status_code not in ok_codes:
            raise GitHubRepoError(
                f"failed to set variable {name} ({r.status_code}): {r.text[:300]}"
            )
        logger.info("set repo variable %s on %s/%s", name, self._owner, self._repo)

    def push(self, secrets: dict[str, str], variables: dict[str, str]) -> None:
        """Push all secrets (encrypted) + variables (plaintext) in one session.
        Raises GitHubRepoError on the first failure (fail-fast, message surfaced
        to the caller so the Portal can show what went wrong)."""
        with httpx.Client(timeout=_HTTP_TIMEOUT) as hc:
            for name, value in secrets.items():
                self.set_secret(hc, name, value)
            for name, value in variables.items():
                self.set_variable(hc, name, value)
