"""Read a GitModel hub's self-reported health.

Exists because a hub's usage-event drop counter had no reader. The counter was
written, `/api/status` returned it, and nothing in the control plane or the
portal ever looked — so when dev-15 lost 21 billing records during a load test,
the only way to find out was to reconcile three data sources by hand. This
module closes that loop.

Shape follows the two hub calls already in `app/api/github_accounts.py`
(`_fetch_hub_models`, `_install_token_on_hub`): blocking `httpx.Client`,
`https://{fqdn}`, `x-admin-token`. The hub's `/api/status` does NOT check that
header today — it is the one unauthenticated `/api/*` route, by design, so the
control plane can poll it — but it is sent anyway so this keeps working if the
route is ever gated.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# Short on purpose: this runs on a timer across every hub, and a hung hub must
# not stall the pass for the others. The two existing hub calls use 30s, but
# they are one-shot admin actions where waiting is the right trade.
STATUS_TIMEOUT = 10.0


@dataclass(frozen=True)
class HubStatus:
    """What one hub says about itself.

    `dropped` counts failed hand-offs (an event that fails three times counts
    3); `lost` counts events given up on for good, each once. They are separate
    on purpose — see `hub/eventhub.py`. `lost` is the billing-data-is-gone
    number and the one worth alerting on.
    """

    logged_in: bool
    dropped: int
    lost: int
    audit_dropped: int
    state: str
    reason: str | None

    @classmethod
    def from_payload(cls, payload: Any) -> HubStatus:
        """Parse `/api/status`, tolerating an older hub that predates the split.

        A hub image is deployed per GitHub account and updated independently of
        the control plane, so at any moment some hubs report the old shape
        (`usage_events_dropped` only). Those degrade to lost=0 / state="ok"
        rather than erroring, which keeps one stale hub from blanking the whole
        pass.
        """
        if not isinstance(payload, dict):
            return cls(False, 0, 0, 0, "unknown", None)
        stats = payload.get("usage_events")
        stats = stats if isinstance(stats, dict) else {}
        recent = stats.get("recent")
        reason = None
        if isinstance(recent, list) and recent:
            last = recent[-1]
            if isinstance(last, dict):
                # Class name only — the hub deliberately never puts an exception
                # MESSAGE on that unauthenticated route.
                bits = [str(last.get("kind") or ""), str(last.get("error") or "")]
                reason = " ".join(b for b in bits if b).strip()[:200] or None
        return cls(
            logged_in=bool(payload.get("logged_in")),
            dropped=_int(payload.get("usage_events_dropped")),
            lost=_int(payload.get("usage_events_lost")),
            audit_dropped=_int(payload.get("audit_payloads_dropped")),
            state=str(stats.get("state") or "ok"),
            reason=reason,
        )


def _int(v: Any) -> int:
    return int(v) if isinstance(v, int) and not isinstance(v, bool) else 0


def fetch_status(fqdn: str, admin_token: str | None) -> HubStatus | None:
    """GET the hub's `/api/status`. Returns None if it could not be reached.

    Blocking — call it via `asyncio.to_thread` from the poller. Never raises:
    an unreachable hub is a fact to record, not an error to propagate into a
    loop that is also responsible for every other hub.
    """
    headers = {"x-admin-token": admin_token} if admin_token else {}
    try:
        with httpx.Client(timeout=STATUS_TIMEOUT) as hc:
            r = hc.get(f"https://{fqdn}/api/status", headers=headers)
            r.raise_for_status()
            return HubStatus.from_payload(r.json())
    except Exception as exc:  # noqa: BLE001 — one bad hub must not fail the pass
        logger.info("hub status unavailable for %s: %s", fqdn, exc)
        return None
