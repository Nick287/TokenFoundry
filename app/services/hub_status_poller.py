"""Poll every deployed hub for its self-reported health, on a timer.

The reader that `app/services/hub_client.py` was written for. That module, the
`hub_status_*` columns, the `hub_status_interval_seconds` setting and the hub's
own `/api/status` route all shipped; nothing ever called them, so the counters
stayed at their defaults and the portal kept rendering whatever the DEPLOY state
machine said.

Which is the part that matters most here. `GitHubAccount.status` reaches READY
when the container deploys and never moves again. It says nothing about whether
the hub can still reach Copilot — and when a hub's OAuth token expires the hub
keeps answering, returning 503 "Hub not logged in to Copilot" to every request.
That 503 is inside the circuit breaker's 5xx range, so APIM sheds the hub for
60 seconds, re-admits it, sheds it again. Traffic does route around it, which is
why nobody notices: the pool quietly runs at 2/3 capacity while all three hubs
show green.

So this loop's job is to make an expired login VISIBLE, and to keep three states
apart that a single boolean would flatten:

    logged in            polled, hub says yes
    login expired        polled, hub says no          -> operator must re-login
    unknown              never polled, or unreachable -> investigate, do not
                                                         assume either way

The last one is why `hub_logged_in` is nullable and why an unreachable hub does
NOT get written as False: "we could not ask" and "we asked and it said no" need
different actions, and collapsing them would send someone to re-login a hub
whose real problem is that it is gone.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from app.db import SessionLocal
from app.models.enums import DeployStatus
from app.models.orm import GitHubAccount
from app.services.hub_client import fetch_status
from app.services.keyvault import KeyVaultService

logger = logging.getLogger(__name__)


def poll_once() -> dict[str, int]:
    """Ask every READY hub how it is, and record the answer.

    Returns counters for the caller to log: {polled, logged_in, expired,
    unreachable}.

    Never raises. It runs on a background timer where an exception kills the
    loop for good, and one unreachable hub must not stop the others being
    polled — so failures are recorded as facts about that hub and the pass
    continues.
    """
    stats = {"polled": 0, "logged_in": 0, "expired": 0, "unreachable": 0}
    db = SessionLocal()
    try:
        accounts = (
            db.query(GitHubAccount)
            .filter(GitHubAccount.status == DeployStatus.READY)
            .all()
        )
        kv = KeyVaultService()
        for acct in accounts:
            if not acct.container_app_fqdn:
                continue
            stats["polled"] += 1
            # /api/status is the hub's one unauthenticated route, so a missing
            # admin token is not a reason to skip the poll — it is sent when we
            # have it purely so this keeps working if the route is ever gated.
            token = None
            if acct.admin_token_kv_ref:
                try:
                    token = kv.get_secret(acct.admin_token_kv_ref)
                except Exception:  # noqa: BLE001 — a KV hiccup must not skip the poll
                    logger.info("hub-status: no admin token for %s", acct.id)

            status = fetch_status(acct.container_app_fqdn, token)
            if status is None:
                # Deliberately leaves hub_logged_in and hub_status_at untouched.
                # The last successful poll's answer, with its timestamp, is more
                # informative than overwriting it with a blank: the portal can
                # then say "logged in, as of 40 minutes ago" instead of losing
                # what it knew the moment the hub went quiet.
                stats["unreachable"] += 1
                logger.warning("hub-status: %s unreachable", acct.id)
                continue

            acct.hub_logged_in = status.logged_in
            acct.hub_status_at = datetime.now(UTC)
            acct.usage_events_dropped = status.dropped
            acct.usage_events_lost = status.lost
            acct.audit_payloads_dropped = status.audit_dropped
            acct.hub_drop_reason = status.reason
            stats["logged_in" if status.logged_in else "expired"] += 1
            if not status.logged_in:
                # Worth a warning even though the portal will show it: this is
                # the state where the hub answers every request with a 503 and
                # the only fix is a human re-authorising the account.
                logger.warning(
                    "hub-status: %s (%s) has lost its Copilot login",
                    acct.id, acct.github_login or "?",
                )
        db.commit()
    except Exception:  # noqa: BLE001 — see the docstring
        db.rollback()
        logger.exception("hub-status: poll failed")
    finally:
        db.close()
    return stats
