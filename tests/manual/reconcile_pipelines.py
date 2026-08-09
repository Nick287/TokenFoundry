#!/usr/bin/env python3
"""Reconcile the four places a call is counted, over ONE absolute time window.

Every other verification script in this repo looks at a single pipeline:
`probe_cosmos_aggregates.py` compares Cosmos against itself, and
`verify_token_three_way.py` compares LlmLog against customMetrics. Nothing joins
the BILLING store to the GATEWAY's own telemetry — which is exactly the gap that
let dev-15 lose 21 usage records with both numbers still "looking normal". That
comparison existed only as prose in docs/CAPACITY.zh.md §7.4, hand-run once.

The four sources and what each can and cannot see:

  gateway (App Insights `requests`)  every request APIM handled, including the
                                     ones it rejected itself (429 from our own
                                     token limit, 503 from the circuit breaker)
  Cosmos                             only calls that REACHED a hub and produced
                                     a usage event; the billing source of truth
  customMetrics                      APIM's own parse of the response body;
                                     independent of the hub->EventHub path, so
                                     it is the one witness that survives a
                                     dropped usage event
  hub /api/status                    each hub's self-reported dropped/lost, the
                                     only place a loss is admitted

CLOSURE, and why it is not "all four numbers are equal":

    gateway 200+429+4xx == cosmos documents + Σ hub lost
    gateway 503         produce no document at all

A 503 never reaches a hub (the breaker is in the gateway), so it produces no
document. A 429 or an upstream 400 DOES reach a hub, which emits an event with
no tokens and no cost. Treating those as errors in the ledger would be wrong
twice over: the call happened, and it cost nothing.

The earlier form paired `gateway 200` against `cosmos_with_tokens` and
`cosmos zero-token` against `gateway 429+4xx`. That held only while every
zero-token document had a matching gateway rejection — true before streaming
refusals were recorded honestly. `StreamingResponse` commits 200 before the
generator contacts upstream, so a refusal there is logged 200 by the gateway
and stored with the upstream status and zero tokens by the hub. On dev-18 that
made both old identities miss by exactly 34 in OPPOSITE directions while the
total was exact — the signature of a stale model rather than of lost data.
Closing on total documents is invariant to where in the request the refusal
landed.

WINDOWS. Both sides are pinned to the same absolute UTC bounds. Cosmos is paged
and bucketed client-side rather than asked for `hours=N`, because `hours` is
relative to query time: comparing a fixed KQL window against a relative Cosmos
window produced two different bogus answers during the dev-15 investigation
before the mistake was spotted.

NO SECRETS IN THIS FILE. Configure via environment (or a git-ignored .env):

    TF_CONTROL_PLANE_URL   the control plane (NOT the gateway)
    TF_ADMIN_USERNAME      default: admin
    TF_ADMIN_PASSWORD
    TF_APP_INSIGHTS_ID     ARM resource id of the App Insights component
                           (optional; without it the gateway/customMetrics
                            columns are reported as unavailable rather than 0)

Usage:
    python tests/manual/reconcile_pipelines.py --since 30
    python tests/manual/reconcile_pipelines.py --from 2026-08-08T09:00:00Z --to 2026-08-08T09:30:00Z
    python tests/manual/reconcile_pipelines.py --since 30 --json out.json

Exit codes: 0 closed, 1 unexplained gap, 2 missing configuration.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

HTTP_TIMEOUT = 180
PAGE_SIZE = 200
MAX_PAGES = 60


# --------------------------------------------------------------------------- #
# Config                                                                       #
# --------------------------------------------------------------------------- #
def load_dotenv_if_present() -> None:
    """Load KEY=VALUE lines from a local .env into os.environ (no override)."""
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except Exception:  # noqa: BLE001 — optional dependency
        pass
    here = Path(__file__).resolve()
    for candidate in (Path.cwd() / ".env", here.parent.parent.parent / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", line.strip())
            if m:
                os.environ.setdefault(m.group(1), m.group(2).strip().strip('"').strip("'"))


def env_or_die(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        print(f"ERROR: {name} is not set (see the module docstring)", file=sys.stderr)
        raise SystemExit(2)
    return v


# --------------------------------------------------------------------------- #
# Control plane                                                                #
# --------------------------------------------------------------------------- #
def cp_call(base: str, path: str, tok: str | None = None, body: dict | None = None):
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "content-type": "application/json",
            **({"authorization": "Bearer " + tok} if tok else {}),
        },
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


# --------------------------------------------------------------------------- #
# App Insights (via az rest — no SDK dependency, matches verify_token_*.py)     #
# --------------------------------------------------------------------------- #
def kql(app_insights_id: str, query: str) -> list[dict] | None:
    """Run one KQL query. None if App Insights is not reachable/configured."""
    url = f"https://api.loganalytics.io/v1{app_insights_id}/query"
    # Resolve the CLI through PATHEXT. On Windows `az` is `az.cmd`, and
    # subprocess (no shell) does NOT try the extensions — so a bare "az" raises
    # FileNotFoundError, which the except below turned into a silent
    # "unavailable". The whole gateway column then read as missing on a machine
    # where az works fine, and the closure check quietly skipped itself.
    az = shutil.which("az")
    if not az:
        print("  (az CLI not found on PATH)")
        return None
    try:
        tok = subprocess.check_output(
            [az, "account", "get-access-token", "--resource",
             "https://api.loganalytics.io", "--query", "accessToken", "-o", "tsv"],
            text=True, stderr=subprocess.DEVNULL,
        ).strip()
    except Exception as exc:  # noqa: BLE001 — not logged in, wrong tenant, ...
        print(f"  (az token request failed: {type(exc).__name__})")
        return None
    req = urllib.request.Request(
        url, data=json.dumps({"query": query}).encode(),
        headers={"Authorization": "Bearer " + tok, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"  (App Insights query failed: HTTP {e.code} {e.read().decode()[:160]})")
        return None
    tables = d.get("tables") or []
    if not tables:
        return []
    cols = [c["name"] for c in tables[0]["columns"]]
    return [dict(zip(cols, row, strict=False)) for row in tables[0]["rows"]]


# --------------------------------------------------------------------------- #
# Collection                                                                   #
# --------------------------------------------------------------------------- #
def collect_gateway(ai_id: str, lo: str, hi: str) -> dict | None:
    rows = kql(ai_id, (
        f"requests | where timestamp >= datetime({lo}) and timestamp < datetime({hi}) "
        "and name startswith 'POST /llm-' "
        "| summarize total=count(), ok=countif(toint(resultCode) < 400), "
        "throttled=countif(toint(resultCode) == 429), "
        "unavailable=countif(toint(resultCode) == 503), "
        "client_err=countif(toint(resultCode) >= 400 and toint(resultCode) < 500 "
        "and toint(resultCode) != 429), "
        "server_err=countif(toint(resultCode) >= 500 and toint(resultCode) != 503)"
    ))
    return rows[0] if rows else None


def collect_custom_metrics(ai_id: str, lo: str, hi: str) -> dict | None:
    rows = kql(ai_id, (
        f"customMetrics | where timestamp >= datetime({lo}) and timestamp < datetime({hi}) "
        "| where name in ('Total Tokens','Prompt Tokens','Completion Tokens',"
        "'Prompt Cached Tokens') "
        "| summarize v=sum(valueSum), n=sum(valueCount) by name"
    ))
    if rows is None:
        return None
    out = {"calls": 0}
    for r in rows:
        out[r["name"]] = int(r["v"])
        out["calls"] = max(out["calls"], int(r["n"]))
    return out


def collect_cosmos(base: str, tok: str, tenant_id: str,
                   lo: datetime, hi: datetime) -> dict:
    """Page the per-call log and bucket client-side on each row's own ts."""
    with_tokens = zero_tokens = 0
    prompt = completion = cached = 0
    by_model_zero: dict[str, int] = defaultdict(int)
    page = 1
    while page <= MAX_PAGES:
        r = cp_call(
            base,
            f"/api/admin/usage/{tenant_id}/records?page={page}&page_size={PAGE_SIZE}&hours=24",
            tok,
        )
        items = r["items"]
        if not items:
            break
        for it in items:
            ts = it.get("ts")
            if not ts:
                continue
            d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if not (lo <= d < hi):
                continue
            p = it["prompt_tok"]
            c = it["completion_tok"]
            ca = it["cached_tok"]
            if p + c + ca > 0:
                with_tokens += 1
                prompt += p
                completion += c
                cached += ca
            else:
                zero_tokens += 1
                by_model_zero[it.get("route") or "?"] += 1
        if len(items) < PAGE_SIZE:
            break
        page += 1
    return {
        "with_tokens": with_tokens,
        "zero_tokens": zero_tokens,
        "prompt": prompt,
        "completion": completion,
        "cached": cached,
        "zero_by_model": dict(by_model_zero),
    }


def collect_hubs(base: str, tok: str) -> list[dict]:
    """Ask every deployed hub what it thinks it lost."""
    out = []
    for a in cp_call(base, "/api/github-accounts", tok) or []:
        fqdn = a.get("container_app_fqdn")
        row = {"id": a["id"], "login": a.get("github_login"), "fqdn": fqdn}
        if not fqdn:
            row["error"] = "no endpoint"
            out.append(row)
            continue
        try:
            req = urllib.request.Request(f"https://{fqdn}/api/status")
            with urllib.request.urlopen(req, timeout=30) as r:
                s = json.loads(r.read())
            stats = s.get("usage_events") or {}
            row.update(
                dropped=int(s.get("usage_events_dropped") or 0),
                lost=int(s.get("usage_events_lost") or 0),
                audit_dropped=int(s.get("audit_payloads_dropped") or 0),
                state=stats.get("state", "?"),
                retried=int(stats.get("retried") or 0),
                recovered=int(stats.get("recovered") or 0),
                retry_queue=int(stats.get("retry_queue") or 0),
                by_reason=stats.get("by_reason") or {},
                recent=stats.get("recent") or [],
                logged_in=bool(s.get("logged_in")),
            )
        except Exception as exc:  # noqa: BLE001 — an unreachable hub is a finding
            row["error"] = f"{type(exc).__name__}: {exc}"
        out.append(row)
    return out


# --------------------------------------------------------------------------- #
# Report                                                                       #
# --------------------------------------------------------------------------- #
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--since", type=int, metavar="MIN",
                    help="window = the last N minutes, ending now")
    ap.add_argument("--from", dest="lo", help="absolute start, e.g. 2026-08-08T09:00:00Z")
    ap.add_argument("--to", dest="hi", help="absolute end")
    ap.add_argument("--json", dest="json_out", metavar="PATH",
                    help="also write the raw numbers here for baseline diffing")
    args = ap.parse_args()

    load_dotenv_if_present()
    base = env_or_die("TF_CONTROL_PLANE_URL").rstrip("/")
    ai_id = os.environ.get("TF_APP_INSIGHTS_ID", "").strip()

    if args.lo and args.hi:
        lo = datetime.fromisoformat(args.lo.replace("Z", "+00:00"))
        hi = datetime.fromisoformat(args.hi.replace("Z", "+00:00"))
    else:
        minutes = args.since or 30
        hi = datetime.now(UTC)
        lo = hi - timedelta(minutes=minutes)
    lo_s = lo.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    hi_s = hi.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"window: {lo_s}  ..  {hi_s}\n")

    tok = cp_call(base, "/api/login", body={
        "username": os.environ.get("TF_ADMIN_USERNAME", "admin"),
        "password": env_or_die("TF_ADMIN_PASSWORD"),
    })["access_token"]
    tenants = cp_call(base, "/api/tenants", tok)
    if not tenants:
        print("ERROR: no tenants on this control plane", file=sys.stderr)
        return 2
    tenant_id = tenants[0]["id"]

    gw = collect_gateway(ai_id, lo_s, hi_s) if ai_id else None
    cm = collect_custom_metrics(ai_id, lo_s, hi_s) if ai_id else None
    cos = collect_cosmos(base, tok, tenant_id, lo, hi)
    hubs = collect_hubs(base, tok)

    print("=== gateway (App Insights `requests`) ===")
    if gw is None:
        print("  unavailable (TF_APP_INSIGHTS_ID unset, or az/App Insights unreachable)")
    else:
        print(f"  total={gw['total']}  200={gw['ok']}  429={gw['throttled']}  "
              f"503={gw['unavailable']}  4xx={gw['client_err']}  5xx={gw['server_err']}")

    print("\n=== Cosmos (billing source) ===")
    print(f"  with tokens={cos['with_tokens']}  zero-token={cos['zero_tokens']}")
    print(f"  prompt={cos['prompt']}  completion={cos['completion']}  cached={cos['cached']}")
    if cos["zero_by_model"]:
        top = sorted(cos["zero_by_model"].items(), key=lambda kv: -kv[1])[:4]
        print("  zero-token by model: " + ", ".join(f"{k}={v}" for k, v in top))

    print("\n=== customMetrics (APIM's own parse — independent of the hub path) ===")
    if cm is None:
        print("  unavailable")
    else:
        print(f"  calls={cm.get('calls')}  prompt={cm.get('Prompt Tokens')}  "
              f"completion={cm.get('Completion Tokens')}  cached={cm.get('Prompt Cached Tokens')}")

    print("\n=== hubs (self-reported) ===")
    total_lost = 0
    for h in hubs:
        if "error" in h:
            print(f"  {h['id']}  {h.get('login')}  UNREACHABLE: {h['error']}")
            continue
        total_lost += h["lost"]
        print(f"  {h['id']}  {h.get('login'):<12} state={h['state']:<11} "
              f"dropped={h['dropped']:<4} lost={h['lost']:<4} "
              f"retried={h['retried']:<4} recovered={h['recovered']:<4} "
              f"queue={h['retry_queue']}")
        if h["recent"]:
            last = h["recent"][-1]
            print(f"      last reason: {last.get('kind')} {last.get('error')} "
                  f"x{last.get('count')} @ {last.get('at')}")
    print(f"  Σ lost = {total_lost}")

    print("\n=== closure ===")
    rc = 0
    if gw is None:
        print("  SKIPPED — the gateway side is unavailable, so nothing can be closed.")
        print("  Set TF_APP_INSIGHTS_ID and run `az login` to make this meaningful.")
    else:
        # The ledger closes on TOTAL documents, not on the served ones alone.
        #
        # The original form was `gateway 200 == cosmos with-tokens + hub lost`,
        # which held only while every zero-token document came from a call the
        # GATEWAY had also rejected. Streaming broke that symmetry: a refusal
        # arriving after StreamingResponse has committed 200 is logged 200 by
        # the gateway and recorded with the upstream status and zero tokens by
        # the hub. dev-18 produced 34 such calls, and the two old identities
        # then missed by 34 in opposite directions while the total was exact —
        # which is the signature of a stale model, not of lost data.
        served_or_refused = cos["with_tokens"] + cos["zero_tokens"]
        lhs = gw["ok"] + gw["throttled"] + gw["client_err"]
        print(f"  gateway 200+429+4xx ({lhs}) == cosmos documents "
              f"({served_or_refused}) + Σ hub lost ({total_lost}) "
              f"= {served_or_refused + total_lost}")
        if lhs == served_or_refused + total_lost:
            print("  ✅ billing ledger closes — every request that reached a hub "
                  "has a document")
        else:
            print(f"  ❌ UNEXPLAINED GAP: {served_or_refused + total_lost - lhs:+d}")
            print("     (Cosmos lags ~90-180s behind a burst, and a settle check "
                  "must outlast a full 300s import cycle — re-run before "
                  "concluding.)")
            rc = 1

        # 503s are shed by the circuit breaker inside the gateway and never
        # reach a hub, so they must produce no document at all. This is the one
        # identity streaming did not disturb.
        print(f"  gateway 503 ({gw['unavailable']}) produce no document: "
              f"{gw['total']} total − {lhs} reached-a-hub = "
              f"{gw['total'] - lhs}")
        if gw["total"] - lhs == gw["unavailable"]:
            print("  ✅ breaker-shed requests correctly absent from billing")
        else:
            print(f"  ⚠️  differs by {gw['total'] - lhs - gw['unavailable']:+d}")

        # Informational: how many refusals arrived too late to change the status
        # line. Non-zero here is the streaming path being honest, not an error.
        late = cos["zero_tokens"] - (gw["throttled"] + gw["client_err"])
        if late > 0:
            print(f"  note: {late} refusal(s) recorded by the hub that the "
                  "gateway logged as 200 — streaming refusals arrive after the "
                  "headers are committed. Expected; see docs/CAPACITY.zh.md.")

    if args.json_out:
        payload = {
            "window": {"from": lo_s, "to": hi_s},
            "gateway": gw, "cosmos": cos, "custom_metrics": cm,
            "hubs": hubs, "total_lost": total_lost,
        }
        Path(args.json_out).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json_out}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
