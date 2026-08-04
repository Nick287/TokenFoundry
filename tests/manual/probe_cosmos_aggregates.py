#!/usr/bin/env python3
"""Probe which query shapes a REAL Cosmos account accepts, then run the actual
aggregation against it.

WHY THIS EXISTS
---------------
The usage aggregation was first written to group server-side (`SUM(...) ...
GROUP BY c.route`). Its unit tests passed — they assert against a fake container,
which can only prove the query TEXT is what we intended — and the deployed
endpoint returned 500 on the first real call:

    (BadRequest) Cross partition query only supports 'VALUE <AggregateFunc>'
                 for aggregates.

No fake can catch that. This script closes the gap: it asks a live account what
it actually executes, and then exercises UsageStore end to end against it.

Run it after ANY change to the aggregation queries, and whenever moving to a new
Cosmos account or SDK version — the supported feature set is a property of the
account and the SDK, not of our code.

Read-only: it issues SELECTs and writes nothing.

Usage:
    export COSMOS_EP="https://<account>.documents.azure.com:443/"
    python tests/manual/probe_cosmos_aggregates.py

Auth is `az login` (AzureCliCredential), so the signed-in user needs the Cosmos
DB Built-in Data Reader role on the account.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

try:
    from azure.cosmos import CosmosClient
    from azure.identity import AzureCliCredential
except ImportError:  # pragma: no cover - manual script
    sys.exit("pip install azure-cosmos azure-identity")

DB = os.environ.get("COSMOS_DB", "tokenfoundry")
CONTAINER = os.environ.get("COSMOS_CONTAINER", "usage")

# Each entry is a shape we might be tempted to write. The point is not that they
# all work — it is to record WHICH fail, so the next person doesn't rediscover it
# in production.
SHAPES = {
    "bare VALUE aggregate": "SELECT VALUE COUNT(1) FROM c",
    "multi-aggregate, no GROUP BY": (
        "SELECT COUNT(1) AS calls, SUM(c.prompt_tok ?? 0) AS p FROM c"
    ),
    "multi-aggregate + GROUP BY": (
        "SELECT c.route AS grp, COUNT(1) AS calls, SUM(c.prompt_tok ?? 0) AS p "
        "FROM c GROUP BY c.route"
    ),
    "single aggregate + GROUP BY": (
        "SELECT c.route AS grp, COUNT(1) AS calls FROM c GROUP BY c.route"
    ),
    "VALUE aggregate + GROUP BY": "SELECT VALUE COUNT(1) FROM c GROUP BY c.route",
    "projection, no aggregate (what we use)": (
        "SELECT c.route, c.prompt_tok, c.cost_usd FROM c ORDER BY c.ts DESC OFFSET 0 LIMIT 5"
    ),
}


def main() -> int:
    endpoint = os.environ.get("COSMOS_EP")
    if not endpoint:
        return int(bool(sys.stderr.write("set COSMOS_EP first\n"))) or 2

    client = CosmosClient(endpoint, credential=AzureCliCredential())
    container = client.get_database_client(DB).get_container_client(CONTAINER)

    print("=" * 72)
    print("Which query shapes does this account accept? (cross-partition)")
    print("=" * 72)
    for name, query in SHAPES.items():
        try:
            rows = list(container.query_items(query=query, enable_cross_partition_query=True))
            print(f"  OK    {name}\n          -> {rows[:2]}")
        except Exception as exc:  # noqa: BLE001 - reporting every failure is the point
            print(f"  FAIL  {name}\n          -> {str(exc).splitlines()[0][:120]}")

    # Now the real thing: UsageStore against this account. If the shapes above
    # changed, this is where it surfaces as an exception rather than a 500 in
    # front of a user.
    print()
    print("=" * 72)
    print("UsageStore against the live account")
    print("=" * 72)
    from app.services.usage_ingest import UsageStore

    store = UsageStore()
    store._endpoint = endpoint
    store._db_name = DB
    store._container_name = CONTAINER

    since = (datetime.now(UTC) - timedelta(hours=24)).isoformat()
    totals = store.cost_totals(None, since_iso=since)
    print(f"  cost_totals(24h): {totals}")

    for dim in UsageStore._AGG_DIMS:
        groups = store.cost_breakdown(None, since_iso=since, group_by=dim)
        print(f"  cost_breakdown(by={dim}): {len(groups)} group(s)")
        for g in groups[:5]:
            print(f"      {g}")

    trend = store.cost_trend(None, since_iso=since, hours=24)
    active = [p for p in trend if p["calls"]]
    print(f"  cost_trend(24h): {len(trend)} buckets, {len(active)} non-empty")
    for p in active[-5:]:
        print(f"      {p}")

    # The totals are computed over every row while the groups can be truncated,
    # so they are allowed to exceed the group sum — but never to fall short.
    group_cost = sum(g["cost_usd"] for g in store.cost_breakdown(None, since_iso=since))
    print()
    print(f"  reconcile: totals={totals['cost_usd']!r} vs sum(groups)={group_cost!r}")
    if totals["cost_usd"] + 1e-9 < group_cost:
        print("  MISMATCH: totals are lower than the groups they summarize")
        return 1
    print("  OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
