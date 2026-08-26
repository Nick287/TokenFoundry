"""Ask the registry what the newest image is, instead of trusting a stale copy.

WHY THIS EXISTS
---------------
A new account's hub gets its image from the `HUB_IMAGE_REF` repo variable, and
that variable is only ever written when an operator presses "推送 GitHub 部署配置"
in the portal. Nothing re-publishes it — not `deploy.sh`, not `update-app.sh`,
not terraform. So the value drifts, silently, and the drift is invisible because
a stale tag is a perfectly valid string: the hub deploys, pulls, and runs. It is
just running last fortnight's code.

Measured on dev-19 (2026-08-26): the variable said `gitmodel:v20260814044101`
while all three live hubs ran `v20260825225134` and the registry held six tags
newer than the one configured. A new account added at that moment would have
come up twelve days behind the fleet.

`tests/test_hub_image_ref.py` already guards the EMPTY tag, which fails loudly at
pull time. This is the other half — the tag that is present, well-formed, and
wrong — and it cannot be caught by inspecting the value. Only the registry knows.

HOW
---
ACR tag listing is a data-plane API with its own token, not an ARM call. The
exchange is three legs:

    ARM token  --POST /oauth2/exchange-->  ACR refresh token
               --POST /oauth2/token----->  ACR access token (scoped to one repo)
               --GET  /acr/v1/{r}/_tags->  the tags

Verified against the live registry on 2026-08-26 that the `pull` scope is enough
to list tags — so the control plane's existing AcrPull role assignment
(terraform/modules/containerapps/main.tf:165) covers this and no new grant is
needed. `metadata_read` also works and is the more obvious scope to reach for,
but asking for it would require a role we do not have.

No new dependency: httpx and DefaultAzureCredential are already used throughout,
and the ARM-token pattern mirrors ApimProvisioner._arm_token.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging

import httpx
from azure.identity import DefaultAzureCredential

logger = logging.getLogger(__name__)

_HTTP_TIMEOUT = 30.0


def _arm_token() -> str:
    return DefaultAzureCredential().get_token("https://management.azure.com/.default").token


def _tenant_of(jwt: str) -> str:
    """Read the tenant id out of the ARM token's `tid` claim.

    The exchange endpoint wants the tenant, and the token already carries it —
    taking it from there keeps this working in any tenant without adding a
    setting that could disagree with the credential actually in use.

    Decode only, never verify: this token was just issued to us by the SDK, and
    we are reading our own claim to address the next request. Signature checking
    belongs to the service that consumes it.
    """
    payload = jwt.split(".")[1]
    payload += "=" * (-len(payload) % 4)  # b64url without padding
    return str(json.loads(base64.urlsafe_b64decode(payload))["tid"])


def newest_tag(registry: str, repository: str) -> str | None:
    """The most recently pushed tag in `repository`, or None if we cannot tell.

    `registry` is the bare ACR name (no .azurecr.io).

    NEVER RAISES. The caller is on the path that creates an account, and failing
    to resolve an image is not a reason to fail the deploy — there is a usable
    fallback (the tag terraform injected). Every failure logs at warning with
    enough detail to act on, because a silent fallback would reintroduce exactly
    the invisible-staleness problem this function exists to fix.

    Ordering is the registry's `timedesc` (push time), NOT a sort of the tag
    strings. Our tags happen to be sortable timestamps, but that is a naming
    convention, not a guarantee, and a hand-pushed tag would break the sort
    without breaking anything else.
    """
    host = f"{registry}.azurecr.io"
    try:
        arm = _arm_token()
        with httpx.Client(timeout=_HTTP_TIMEOUT) as hc:
            refresh = hc.post(
                f"https://{host}/oauth2/exchange",
                data={
                    "grant_type": "access_token",
                    "service": host,
                    "tenant": _tenant_of(arm),
                    "access_token": arm,
                },
            )
            if refresh.status_code != 200:
                logger.warning(
                    "acr: token exchange on %s failed (%s): %s",
                    host, refresh.status_code, refresh.text[:200],
                )
                return None

            access = hc.post(
                f"https://{host}/oauth2/token",
                data={
                    "grant_type": "refresh_token",
                    "service": host,
                    "scope": f"repository:{repository}:pull",
                    "refresh_token": refresh.json()["refresh_token"],
                },
            )
            if access.status_code != 200:
                logger.warning(
                    "acr: could not get a pull-scoped token for %s/%s (%s): %s",
                    host, repository, access.status_code, access.text[:200],
                )
                return None

            tags = hc.get(
                f"https://{host}/acr/v1/{repository}/_tags",
                params={"orderby": "timedesc", "n": 1},
                headers={"Authorization": f"Bearer {access.json()['access_token']}"},
            )
            if tags.status_code != 200:
                logger.warning(
                    "acr: listing tags of %s/%s failed (%s): %s",
                    host, repository, tags.status_code, tags.text[:200],
                )
                return None

        found = tags.json().get("tags") or []
        if not found:
            logger.warning("acr: repository %s/%s has no tags", host, repository)
            return None
        name = str(found[0]["name"])
        logger.info("acr: newest %s/%s is %s", host, repository, name)
        return name
    except (httpx.HTTPError, KeyError, ValueError, TypeError, binascii.Error) as exc:
        logger.warning("acr: could not resolve newest %s/%s: %s", host, repository, exc)
        return None
    except Exception:  # noqa: BLE001 — see the NEVER RAISES contract above
        logger.exception("acr: unexpected failure resolving newest %s/%s", host, repository)
        return None
