"""Usage ingest + read.

Phase 1: pull token metrics from Application Insights via KQL (azure-monitor-
query), aggregate, and persist raw records to Cosmos DB for NoSQL.
Phase 2: switch the source of truth to an Event Hub consumer (worker/) for
billing-grade, replayable accounting.

This module owns the Cosmos write path and the KQL read path. Billing math is
delegated to app.services.billing.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta

from azure.cosmos import CosmosClient, PartitionKey
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from app.config import get_settings
from app.models.schemas import UsageRecord

logger = logging.getLogger(__name__)


def _parse_usage_tokens(usage_json: str | None) -> dict[str, int]:
    """Parse a raw provider `usage` JSON string (from the usage trace) into a
    dict of token counts keyed like _TOKEN_KEYS (minus `total`, which the caller
    derives): prompt, completion, cached, reasoning, cache_creation,
    accepted_prediction, rejected_prediction, prompt_audio, completion_audio.

    Provider-agnostic — handles every provider's field names:
      * Anthropic: input_tokens / output_tokens / cache_read_input_tokens /
        cache_creation_input_tokens
      * OpenAI/Google chat: prompt_tokens / completion_tokens /
        prompt_tokens_details.{cached_tokens,audio_tokens} /
        completion_tokens_details.{reasoning_tokens,accepted_prediction_tokens,
        rejected_prediction_tokens,audio_tokens}
      * Google: top-level reasoning_tokens (whole output is reasoning)
    Non-JSON / "BODY_READ_FAILED" (streaming) → all zeros. `cached` is the
    cache-READ tokens (a subset of input); `cache_creation` is the cache-WRITE
    tokens (anthropic-only, billed higher).
    """
    zero = {
        "total": 0,
        "prompt": 0, "completion": 0, "cached": 0, "reasoning": 0,
        "cache_creation": 0, "accepted_prediction": 0,
        "rejected_prediction": 0, "prompt_audio": 0, "completion_audio": 0,
    }
    if not usage_json or usage_json in ("BODY_READ_FAILED", "NO_USAGE_KEY"):
        return dict(zero)
    try:
        u = json.loads(usage_json)
    except (ValueError, TypeError):
        return dict(zero)
    if not isinstance(u, dict):
        return dict(zero)

    def _i(v: object) -> int:
        try:
            return int(v)  # type: ignore[call-overload]
        except (ValueError, TypeError):
            return 0

    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    od = u.get("output_tokens_details") or {}

    out = dict(zero)
    out["prompt"] = _i(u.get("input_tokens", u.get("prompt_tokens", 0)))
    out["completion"] = _i(u.get("output_tokens", u.get("completion_tokens", 0)))
    out["cached"] = _i(u.get("cache_read_input_tokens") or pd.get("cached_tokens") or 0)
    out["cache_creation"] = _i(u.get("cache_creation_input_tokens", 0))
    out["reasoning"] = _i(
        # Google puts thinking tokens at the TOP level as reasoning_tokens (with
        # completion_tokens=0 — the whole output is reasoning). OpenAI nests it in
        # completion_tokens_details.reasoning_tokens (a SUBSET of completion).
        u.get("reasoning_tokens")
        or cd.get("reasoning_tokens")
        or od.get("reasoning_tokens")
        or 0
    )
    out["accepted_prediction"] = _i(cd.get("accepted_prediction_tokens", 0))
    out["rejected_prediction"] = _i(cd.get("rejected_prediction_tokens", 0))
    out["prompt_audio"] = _i(pd.get("audio_tokens", 0))
    out["completion_audio"] = _i(cd.get("audio_tokens", 0))
    # Google reports the visible output under reasoning_tokens with
    # completion_tokens=0, so its "output" is really the reasoning. Fold it into
    # completion when completion is 0 but reasoning is present, so total
    # (= prompt + completion) matches the provider's total_tokens. For OpenAI,
    # reasoning is already inside completion, so we DON'T add it again.
    if out["completion"] == 0 and out["reasoning"] > 0:
        out["completion"] = out["reasoning"]

    # `total` — the ONE number that must match the provider's own accounting.
    # Providers differ in what `prompt` includes, so a naive prompt+completion is
    # wrong for anthropic:
    #   * OpenAI/Google give an authoritative `total_tokens` (prompt already
    #     INCLUDES cached, so we must NOT re-add cache_* or we'd double count).
    #   * Anthropic has no total_tokens and its `input_tokens` EXCLUDES the cache
    #     reads/writes, so the true total is input + output + cache_read +
    #     cache_creation. (This is what makes cache_creation billable input show
    #     up in the total instead of being dropped.)
    upstream_total = _i(u.get("total_tokens", 0))
    if upstream_total > 0:
        out["total"] = upstream_total
    else:
        out["total"] = (
            out["prompt"] + out["completion"] + out["cached"] + out["cache_creation"]
        )
    return out


class UsageStore:
    """Cosmos DB for NoSQL writer/reader for raw usage records."""

    def __init__(self) -> None:
        s = get_settings()
        self._endpoint = s.cosmos_endpoint
        self._db_name = s.cosmos_database
        self._container_name = s.cosmos_usage_container
        self._client: CosmosClient | None = None

    @property
    def _container(self):  # noqa: ANN202 - azure sdk returns untyped proxy
        if self._client is None:
            self._client = CosmosClient(
                self._endpoint, credential=DefaultAzureCredential()
            )
        db = self._client.create_database_if_not_exists(self._db_name)
        return db.create_container_if_not_exists(
            id=self._container_name,
            partition_key=PartitionKey(path="/pk"),
        )

    def write(self, record: UsageRecord) -> None:
        item = record.model_dump(mode="json")
        item["pk"] = record.partition_key()
        item["id"] = record.request_id
        self._container.upsert_item(item)

    def upsert(self, doc: dict) -> None:
        """Upsert a pre-shaped usage document (already carrying `id` and `pk`).

        The batch importer builds its own document rather than going through
        UsageRecord, because it carries fields UsageRecord has no room for (the
        verbatim upstream `copilot_usage`, the end-user tag, streaming flag).
        Upsert, not create: Event Hub Capture is at-least-once, so the same
        request id can arrive twice and overwriting is what makes re-imports
        free of duplicates.
        """
        self._container.upsert_item(doc)

    def query_tenant(self, tenant_id: str, limit: int = 1000) -> list[dict]:
        # Local/dev without a Cosmos account configured: return empty instead of
        # constructing a CosmosClient("") that would throw. Keeps /usage and
        # /admin/usage working (zero-valued summary) so the portal renders.
        if not self._endpoint:
            logger.info(
                "usage: no cosmos endpoint configured; returning empty usage"
            )
            return []
        # Records written by the APIM outbound policy carry `tenant` (and, until a
        # tenant header is wired, the literal "unknown"); legacy/worker records may
        # carry `tenant_id`. Match either so both shapes are queryable.
        query = (
            "SELECT * FROM c WHERE c.tenant_id = @t OR c.tenant = @t "
            "ORDER BY c.ts DESC OFFSET 0 LIMIT @n"
        )
        return list(
            self._container.query_items(
                query=query,
                parameters=[
                    {"name": "@t", "value": tenant_id},
                    {"name": "@n", "value": limit},
                ],
                enable_cross_partition_query=True,
            )
        )

    def query_all(self, limit: int = 1000) -> list[dict]:
        """All usage records (admin cross-tenant). Used while tenant tagging is
        still 'unknown' so the portal can show real data regardless of tenant."""
        if not self._endpoint:
            return []
        return list(
            self._container.query_items(
                query="SELECT * FROM c ORDER BY c.ts DESC OFFSET 0 LIMIT @n",
                parameters=[{"name": "@n", "value": limit}],
                enable_cross_partition_query=True,
            )
        )

    def query_by_subscriptions(
        self,
        subscription_ids: list[str],
        limit: int = 1000,
        skip: int = 0,
        since_iso: str | None = None,
    ) -> list[dict]:
        """Usage records whose `subscription` (virtual key id) is in the given
        set. This is how a tenant's usage is resolved: the caller maps tenant ->
        its virtual keys via PostgreSQL, then we match those keys in Cosmos.
        Records are written with tenant='unknown' (no tenant header on the data
        plane yet), so the virtual key is the reliable tenant linkage.

        `skip`/`limit` give server-side pagination (OFFSET/LIMIT) for the portal
        call log; pair with count_by_subscriptions for total pages.

        `since_iso=None` means all time. It exists because the call log and the
        breakdown below it used to disagree: this query had no time filter while
        the breakdown hard-coded 24h, so a page could show a full call log above
        "no usage in this window" — which reads as a broken dashboard, not as two
        different questions."""
        if not self._endpoint or not subscription_ids:
            return []
        where, params = self._agg_where(subscription_ids, since_iso)
        return list(
            self._container.query_items(
                query=f"SELECT * FROM c{where} ORDER BY c.ts DESC OFFSET @skip LIMIT @n",
                parameters=[
                    *params,
                    {"name": "@skip", "value": skip},
                    {"name": "@n", "value": limit},
                ],
                enable_cross_partition_query=True,
            )
        )

    def count_by_subscriptions(
        self, subscription_ids: list[str], since_iso: str | None = None
    ) -> int:
        """Total number of usage records for the given virtual keys — used to
        compute page count for the paginated call log. Must apply the SAME window
        as query_by_subscriptions or the pager offers pages that come back empty.
        Returns 0 when Cosmos is not configured or the key set is empty."""
        if not self._endpoint or not subscription_ids:
            return 0
        where, _params = self._agg_where(subscription_ids, since_iso)
        # A bare `SELECT VALUE COUNT(1)` is the only aggregate shape Cosmos
        # accepts cross-partition (see the note above _MAX_ROWS).
        rows = list(
            self._container.query_items(
                query=f"SELECT VALUE COUNT(1) FROM c{where}",
                parameters=_params,
                enable_cross_partition_query=True,
            )
        )
        return int(rows[0]) if rows else 0

    # ----------------------------------------------------------------- #
    # Server-side aggregation (the billing-grade breakdown)              #
    # ----------------------------------------------------------------- #
    # Requested dimension -> the document field it groups on. A WHITELIST, not a
    # convenience map: the value is interpolated into the query text (Cosmos has
    # no bind parameter for an identifier), so anything reaching the SQL string
    # must come from these literals and never from the caller.
    _AGG_DIMS = {
        "model": "route",
        "api": "api",
        "subscription": "subscription",
        "backend": "hub_id",
        # Cosmos-only: App Insights never saw the end user, because the metric
        # policy can't read the request body.
        "end_user": "end_user",
    }

    # Per-type token columns, in the order the portal renders them.
    _TOKEN_FIELDS = ("prompt_tok", "cached_tok", "cache_write_tok", "completion_tok")

    # Why these aggregate in Python rather than in Cosmos
    # ---------------------------------------------------
    # Cosmos NoSQL rejects every cross-partition aggregate except a bare
    # `SELECT VALUE <AggregateFunc>`:
    #
    #   SELECT COUNT(1) AS calls, SUM(...) AS p FROM c
    #     -> BadRequest: "Cross partition query only supports 'VALUE
    #        <AggregateFunc>' for aggregates."
    #   ...and adding GROUP BY fails the same way; `SELECT VALUE COUNT(1) ...
    #        GROUP BY` is rejected as an unsupported feature.
    #
    # (Verified against the deployed account, not inferred from the docs.) The
    # partition key is `<subscription>_<yyyyMM>`, so ANY breakdown spans
    # partitions by construction — a tenant has several keys, and a window can
    # cross a month. Server-side grouping is therefore not available to us at
    # all, and the honest implementation is to project the few columns we need
    # and fold them here.
    #
    # The cost of that is bounded by _MAX_ROWS below, and it is a real cost: the
    # rows travel over the wire. It is nonetheless the correct trade, because
    # the alternative shapes do not execute.
    #
    # Only the projected columns are fetched, never `SELECT *` — the documents
    # also carry the verbatim upstream `copilot_usage` and the raw provider
    # `usage` blob, which would dominate the payload and are not needed here.
    _MAX_ROWS = 20000

    # Cap on distinct groups RETURNED (not scanned). Group counts are naturally
    # small for model/api/subscription; `end_user` is whatever the customer sent
    # and has no natural bound.
    _MAX_GROUPS = 500

    def _agg_where(
        self, subscription_ids: list[str] | None, since_iso: str | None
    ) -> tuple[str, list[dict]]:
        """WHERE clause + parameters shared by every aggregate below.

        `subscription_ids=None` means platform-wide (admin); an EMPTY list is a
        different thing entirely — a tenant with no keys — and the callers
        short-circuit it before getting here rather than letting it degrade into
        an unfiltered query over every tenant's data.
        """
        clauses: list[str] = []
        params: list[dict] = []
        if subscription_ids:
            clauses.append("ARRAY_CONTAINS(@ids, c.subscription)")
            params.append({"name": "@ids", "value": subscription_ids})
        if since_iso:
            # String comparison over ISO-8601. Sound only because _parse_ts
            # normalizes every stored ts to UTC (see usage_capture_import).
            clauses.append("c.ts >= @since")
            params.append({"name": "@since", "value": since_iso})
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return where, params

    def _fetch_agg_rows(
        self,
        subscription_ids: list[str] | None,
        since_iso: str | None,
        extra_fields: tuple[str, ...] = (),
    ) -> list[dict]:
        """Project the columns an aggregate needs, newest first, capped.

        Ordered by ts DESC so that if the cap does bite, what survives is the
        most recent window rather than an arbitrary slice — a partial view of
        "just now" is explicable to a user; a partial view of nothing in
        particular is not.
        """
        cols = ", ".join(
            f"c.{f}"
            for f in (
                *self._TOKEN_FIELDS,
                "cost_usd",
                "billed_usd",
                # `status` is what separates a served call from one upstream
                # refused. Every aggregate needs it, so it is projected here
                # rather than passed in as an extra_field by each caller.
                "status",
                *extra_fields,
            )
        )
        where, params = self._agg_where(subscription_ids, since_iso)
        params = [*params, {"name": "@n", "value": self._MAX_ROWS}]
        rows = list(
            self._container.query_items(
                query=f"SELECT {cols} FROM c{where} ORDER BY c.ts DESC OFFSET 0 LIMIT @n",
                parameters=params,
                enable_cross_partition_query=True,
            )
        )
        if len(rows) >= self._MAX_ROWS:
            # Never silently. At the cap the numbers below are a floor, not a
            # total, and a dashboard that says so is worth more than one that
            # quietly under-reports spend.
            logger.warning(
                "usage aggregation hit the %d-row cap; totals are partial for this window",
                self._MAX_ROWS,
            )
        return rows

    @staticmethod
    def _blank(fields: tuple[str, ...]) -> dict:
        return {
            "calls": 0,
            # Split out of `calls` because "1188 calls" hid 46 upstream
            # rejections during the dev-16 campaign: they cost nothing, so the
            # money was right, but the customer-facing call count was not. The
            # split reads off the `status` the hub already records — it was in
            # every document from the start and simply never projected.
            "ok_calls": 0,
            "failed_calls": 0,
            # Per-status-code counts, e.g. {"429": 67}. A bare failure total
            # cannot be reconciled against the gateway: an upstream throttle and
            # a malformed request are both "failed" here, and only the former
            # has a counterpart in the App Insights `requests` table. A fresh
            # dict per bucket — sharing one would merge every group's errors.
            "failed_by_status": {},
            **dict.fromkeys(fields, 0),
            "cost_usd": 0.0,
            "billed_usd": 0.0,
        }

    @classmethod
    def _accumulate(cls, bucket: dict, row: dict) -> None:
        """Fold one document into a running bucket.

        `or 0` on every read is load-bearing rather than defensive: a document
        written before a column existed carries JSON null for it (verified —
        legacy rows have `cache_write_tok: null`), and `null + int` raises.
        """
        bucket["calls"] += 1
        # A document written before `status` existed has None here. Treating
        # that as a failure would retroactively invent errors in historical
        # data, so an unknown status counts as OK — the same way it read before
        # this split existed.
        status = row.get("status")
        if isinstance(status, int) and status >= 400:
            bucket["failed_calls"] += 1
            # Keyed by string: this dict is JSON, and JSON object keys are
            # strings anyway — doing it here keeps the portal from having to
            # handle both forms.
            by_status = bucket["failed_by_status"]
            key = str(status)
            by_status[key] = by_status.get(key, 0) + 1
        else:
            bucket["ok_calls"] += 1
        for f in cls._TOKEN_FIELDS:
            bucket[f] += int(row.get(f) or 0)
        bucket["cost_usd"] += float(row.get("cost_usd") or 0.0)
        bucket["billed_usd"] += float(row.get("billed_usd") or 0.0)

    def cost_breakdown(
        self,
        subscription_ids: list[str] | None = None,
        since_iso: str | None = None,
        group_by: str = "model",
    ) -> list[dict]:
        """Per-group token + cost totals.

        This is the billing-grade breakdown: `cost_usd`/`billed_usd` come from
        upstream's own `total_nano_aiu` (recorded per call at import time), and
        streaming calls are included on equal terms — the two things the App
        Insights breakdown structurally cannot do.

        Returns rows shaped {<group_by>, calls, prompt_tok, cached_tok,
        cache_write_tok, completion_tok, cost_usd, billed_usd}, most expensive
        first. [] when Cosmos isn't configured, the tenant has no keys, or
        `group_by` isn't a known dimension.
        """
        if not self._endpoint or subscription_ids == []:
            return []
        field = self._AGG_DIMS.get(group_by)
        if not field:
            return []

        rows = self._fetch_agg_rows(subscription_ids, since_iso, extra_fields=(field,))
        buckets: dict[str, dict] = {}
        for r in rows:
            key = str(r.get(field) or "unknown")
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {group_by: key, **self._blank(self._TOKEN_FIELDS)}
                buckets[key] = bucket
            self._accumulate(bucket, r)

        out = sorted(buckets.values(), key=lambda d: d["cost_usd"], reverse=True)
        if len(out) > self._MAX_GROUPS:
            # Say what was dropped. A silently truncated list reads as "this is
            # everything", and the totals (computed over every row) would then
            # not equal the sum of the rows shown.
            logger.warning(
                "cost_breakdown by=%s produced %d groups; showing the %d costliest",
                group_by, len(out), self._MAX_GROUPS,
            )
            out = out[: self._MAX_GROUPS]
        return out

    def cost_totals(
        self,
        subscription_ids: list[str] | None = None,
        since_iso: str | None = None,
    ) -> dict:
        """Window totals.

        Computed over every row rather than by summing `cost_breakdown` so the
        headline numbers stay whole even when the group list was truncated, and
        so a NULL dimension (an unset end_user) can't drop calls from the total.
        """
        empty = self._blank(self._TOKEN_FIELDS)
        if not self._endpoint or subscription_ids == []:
            return empty

        totals = self._blank(self._TOKEN_FIELDS)
        for r in self._fetch_agg_rows(subscription_ids, since_iso):
            self._accumulate(totals, r)
        return totals

    # Hourly buckets stop being readable long before the window stops being
    # useful: 30 days is 720 points, which renders as noise and asks the x-axis
    # for 720 slots. The threshold sits just past 48h so the common "yesterday
    # and today" window keeps hour resolution.
    _HOURLY_MAX_HOURS = 48

    @classmethod
    def trend_bucket(cls, hours: int) -> str:
        """Granularity `cost_trend` will use for a window of `hours`: hour|day.

        Exposed so the API can tell the portal which one it got — the chart
        labels a daily point as a date and an hourly one as a time, and guessing
        from the spacing would break on a window with a single bucket.
        """
        return "hour" if hours <= cls._HOURLY_MAX_HOURS else "day"

    def cost_trend(
        self,
        subscription_ids: list[str] | None = None,
        since_iso: str | None = None,
        hours: int = 24,
    ) -> list[dict]:
        """Tokens/calls/cost series over the window, zero-filled, oldest first.

        Buckets on a prefix of the stored timestamp — 13 chars for an hour
        ("2026-08-04T10"), 10 for a day ("2026-08-04") — which IS the UTC bucket
        because every ts is normalized to UTC at import. The zero-fill matters:
        without it a quiet bucket vanishes and the chart draws a straight line
        across the gap, implying traffic that never happened.
        """
        if not self._endpoint or subscription_ids == []:
            return []

        bucket = self.trend_bucket(hours)
        hourly = bucket == "hour"
        width, fmt = (13, "%Y-%m-%dT%H") if hourly else (10, "%Y-%m-%d")

        rows = self._fetch_agg_rows(subscription_ids, since_iso, extra_fields=("ts",))
        by_bucket: dict[str, dict] = {}
        for r in rows:
            ts = r.get("ts")
            if not isinstance(ts, str):
                continue
            slot = by_bucket.setdefault(ts[:width], self._blank(self._TOKEN_FIELDS))
            self._accumulate(slot, r)

        now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        if not hourly:
            now = now.replace(hour=0)
        step = timedelta(hours=1) if hourly else timedelta(days=1)
        # Round up: a 72h window is 3 days, a 70h window still spans 3 day
        # buckets. Under-counting would clip the oldest bucket off the chart.
        count = max(hours, 1) if hourly else max(-(-max(hours, 1) // 24), 1)

        out: list[dict] = []
        for i in range(count - 1, -1, -1):
            slot_start = now - step * i
            got = by_bucket.get(slot_start.strftime(fmt))
            out.append(
                {
                    "ts": slot_start.isoformat(),
                    # Every token type, cache_write included — omitting it would
                    # make cache-heavy buckets read as a dip in the chart.
                    "tokens": (
                        sum(got[f] for f in self._TOKEN_FIELDS) if got else 0
                    ),
                    "calls": got["calls"] if got else 0,
                    "cost_usd": got["cost_usd"] if got else 0.0,
                }
            )
        return out


class AppInsightsUsage:
    """Phase 1 KQL pull of llm-emit-token-metric custom metrics."""

    def __init__(self) -> None:
        self._resource_id = get_settings().app_insights_resource_id
        self._client = (
            LogsQueryClient(credential=DefaultAzureCredential())
            if self._resource_id
            else None
        )

    def request_telemetry(self, hours: int = 24) -> dict:
        """Per-API call counts + latency from App Insights `requests` table.

        Returns a summary the portal's "calls & latency" block renders. Three
        independent queries, each best-effort and merged:
          * base  — calls / p50 / p95 / failures, by API (the "which model most"
                    answer: rows are ordered by call count)
          * split — gateway vs backend duration, by API (requests↔dependencies)
          * codes — calls per HTTP status code, so a failure total can be
                    reconciled against the Cosmos one instead of merely
                    disagreeing with it
          * trend — calls per bucket over the window (hourly, or daily once the
                    window is long enough that hourly points stop being legible)
        Each query degrades independently: if the fragile dependency join yields
        nothing, the base latency table still renders and the split columns show
        as null. App Insights telemetry is best-effort, separate from Cosmos
        usage which is the billing source.
        """
        empty: dict = {
            "by_api": [],
            "total_calls": 0,
            "total_ok": 0,
            "total_failures": 0,
            "by_status": [],
            "by_hour": [],
            "bucket": UsageStore.trend_bucket(hours),
            "hours": hours,
        }
        if not self._client or not self._resource_id:
            return empty

        # 1) Base: calls + latency + failures, ordered so the busiest API is first.
        base_kql = """
        requests
        | where name startswith 'POST /llm-'
        | summarize calls = count(),
                    p50 = percentile(duration, 50),
                    p95 = percentile(duration, 95),
                    failures = countif(toint(resultCode) >= 400)
                    by name
        | order by calls desc
        """
        by_api = self._run_kql(base_kql, hours)
        if not by_api:
            return empty

        # 2) Split: gateway (APIM) vs backend (LLM) time. Total request duration
        #    minus the summed backend dependency duration per operation. Runs as a
        #    SEPARATE query so a missing/empty dependencies table can't blank the
        #    base table — the columns just render null.
        split_kql = """
        let deps = dependencies
            | summarize depDur = sum(duration) by opId = operation_Id;
        requests
        | where name startswith 'POST /llm-'
        | project opId = operation_Id, name, reqDur = duration
        | join kind=leftouter deps on opId
        | extend backendDur = coalesce(depDur, 0.0)
        | extend gatewayDur = reqDur - backendDur
        | summarize gateway_p50 = percentile(gatewayDur, 50),
                    backend_p50 = percentile(backendDur, 50)
                    by name
        """
        split_by_name = {r.get("name"): r for r in self._run_kql(split_kql, hours)}
        for row in by_api:
            split = split_by_name.get(row.get("name"))
            row["gateway_p50"] = split.get("gateway_p50") if split else None
            row["backend_p50"] = split.get("backend_p50") if split else None

        # 3) Codes: one row per HTTP status the gateway returned. This is the
        #    half of the reconciliation Cosmos cannot supply — a request the
        #    circuit breaker sheds never reaches a hub, so it produces no usage
        #    document at all. Without this table the two failure totals simply
        #    disagree with no way to see why.
        codes_kql = """
        requests
        | where name startswith 'POST /llm-'
        | summarize calls = count() by resultCode
        | order by calls desc
        """
        by_status = [
            {"status": str(r.get("resultCode") or "?"),
             "calls": int(r.get("calls", 0) or 0)}
            for r in self._run_kql(codes_kql, hours)
        ]

        # 4) Trend: calls per bucket, oldest→newest. make-series zero-fills the
        #    gaps so the chart shows a continuous timeline (a plain summarize
        #    by bin() only emits buckets that had calls — producing a few isolated
        #    spikes with empty space between, not a real time series).
        #
        #    The range and step track `hours` rather than being fixed at 24h/1h:
        #    they used to be hard-coded, so a 7-day window rendered a 7-day table
        #    above a 24-hour chart with no indication the two disagreed. Step
        #    switches to daily on the same threshold Cosmos uses, so both charts
        #    on the page share a granularity.
        #    `hours` is an int clamped by the caller, so interpolating it here is
        #    not a KQL-injection surface; int() makes that guarantee local.
        step = "1h" if hours <= UsageStore._HOURLY_MAX_HOURS else "1d"
        trend_kql = f"""
        requests
        | where name startswith 'POST /llm-'
        | make-series calls = count() default = 0
            on timestamp from ago({int(hours)}h) to now() step {step}
        | mv-expand timestamp to typeof(datetime), calls to typeof(long)
        | order by timestamp asc
        """
        by_hour = [
            {"ts": str(r.get("timestamp")), "calls": int(r.get("calls", 0) or 0)}
            for r in self._run_kql(trend_kql, hours)
        ]

        total = sum(int(r.get("calls", 0) or 0) for r in by_api)
        # Derived from the per-API `failures` column rather than from
        # `by_status`, so the headline still holds if the codes query is the one
        # that degrades. A status that won't parse as an int (App Insights
        # records client-side aborts as "0" and occasionally as text) counts as
        # a failure — it is certainly not a served 200.
        failures = sum(int(r.get("failures", 0) or 0) for r in by_api)
        return {
            "by_api": by_api,
            "total_calls": total,
            "total_ok": total - failures,
            "total_failures": failures,
            "by_status": by_status,
            "by_hour": by_hour,
            "bucket": UsageStore.trend_bucket(hours),
            "hours": hours,
        }

    def _run_kql(self, kql: str, hours: int) -> list[dict]:
        """Run one KQL query over the App Insights resource; [] on any failure.

        Each telemetry sub-query calls this independently so a single failure
        (e.g. an empty dependencies table) degrades just that slice.
        """
        if not self._client or not self._resource_id:
            return []
        try:
            response = self._client.query_resource(
                self._resource_id, kql, timespan=timedelta(hours=hours)
            )
        except Exception:  # noqa: BLE001 — telemetry is best-effort
            logger.warning("App Insights telemetry query failed", exc_info=True)
            return []
        if response.status != LogsQueryStatus.SUCCESS or not response.tables:
            return []
        table = response.tables[0]
        return [dict(zip(table.columns, row, strict=False)) for row in table.rows]

    # App Insights custom-metric names emitted by APIM's llm-emit-token-metric
    # (verified live on dev-a03, StandardV2 — exactly these 9 names). Mapped to
    # short, provider-neutral keys. `valueSum` holds the token count. Dimensions:
    # subscription (virtual key id), api (llm-<provider>), model.
    # The last four (prediction/audio) are 0 for plain-text calls but emitted for
    # multimodal / speculative-decoding scenarios, so we surface them too.
    # NOTE: cache_creation (anthropic's cache-WRITE tokens) is NOT a customMetric —
    # APIM only emits "Prompt Cached Tokens" (the read). It exists only in the raw
    # usage JSON, so the traces/backend path fills it; the customMetrics path
    # leaves it 0. That's why it's in _TOKEN_KEYS but not _TOKEN_METRICS.
    _TOKEN_METRICS = {
        "total": "Total Tokens",
        "prompt": "Prompt Tokens",
        "cached": "Prompt Cached Tokens",
        "completion": "Completion Tokens",
        "reasoning": "Completion Reasoning Tokens",
        "accepted_prediction": "Completion Accepted Prediction Tokens",
        "rejected_prediction": "Completion Rejected Prediction Tokens",
        "prompt_audio": "Prompt Audio Tokens",
        "completion_audio": "Completion Audio Tokens",
    }
    # Every per-token-type numeric key a breakdown row carries (order = frontend
    # column order). Includes cache_creation, which only the traces path fills.
    # `calls` is added separately.
    _TOKEN_KEYS = (
        "total", "prompt", "cached", "completion", "reasoning",
        "cache_creation", "accepted_prediction", "rejected_prediction",
        "prompt_audio", "completion_audio",
    )

    @staticmethod
    def _sub_filter(subscription_ids: list[str] | None) -> str:
        """KQL fragment restricting to a set of virtual-key ids (the `subscription`
        dimension). Empty/None → no filter (admin, all keys). Ids are our own
        opaque `vk_...`/GUID strings (no user input), quoted into a dynamic set."""
        if not subscription_ids:
            return ""
        quoted = ", ".join(f'"{s}"' for s in subscription_ids)
        return (
            "| extend subscription = tostring(customDimensions['subscription']) "
            f"| where subscription in ({quoted}) "
        )

    def _metric_names_kql(self) -> str:
        """`name in (...)` fragment covering exactly the token metrics we surface."""
        quoted = ", ".join(f'"{m}"' for m in self._TOKEN_METRICS.values())
        return f"| where name in ({quoted}) "

    # Dimensions the breakdown can group by → the customDimensions key each maps
    # to. All three are emitted by llm-emit-token-metric.
    _GROUP_DIMS = {
        "model": "model",
        "api": "api",
        "subscription": "subscription",
    }

    def token_usage_breakdown(
        self,
        subscription_ids: list[str] | None = None,
        hours: int = 24,
        group_by: str = "model",
    ) -> list[dict]:
        """Per-group, per-token-type token totals from App Insights customMetrics.

        Covers BOTH streaming and non-streaming calls (llm-emit-token-metric runs
        inside the pipeline, independent of the Cosmos write which skips SSE).

        `group_by` is one of "model" (default), "api" (endpoint), or
        "subscription" (virtual key). Each returned row is shaped
        {"<group>", "total", "prompt", "cached", "completion", "reasoning",
        "calls"} — one row per group value. `calls` is the metered call count
        (sum of valueCount on the Total Tokens metric). Restricted to
        `subscription_ids` when given (a tenant's keys); unrestricted for admin.
        [] if App Insights isn't configured or group_by is unknown.
        """
        if self._client is None or not self._resource_id:
            return []
        # "backend" (the real per-account hub) is NOT a customMetrics dimension —
        # llm-emit-token-metric can't see the pool member. It lives only in our
        # usage `trace` (decoded from the session-affinity cookie). So route it to
        # the traces-based path; all other dims come from customMetrics.
        if group_by == "backend":
            return self._backend_breakdown(subscription_ids, hours)
        group = self._GROUP_DIMS.get(group_by)
        if not group:
            return []
        kql = (
            "customMetrics "
            + self._metric_names_kql()
            + self._sub_filter(subscription_ids)
            + f"| extend {group} = tostring(customDimensions['{group}']) "
            f"| summarize tokens = sum(valueSum), calls = sum(valueCount) "
            f"by metric = name, {group} "
            f"| order by {group} asc"
        )
        rows = self._run_kql(kql, hours)
        # Pivot the (metric, group)->tokens long form into one dict per group value.
        name_to_key = {v: k for k, v in self._TOKEN_METRICS.items()}
        out: dict[str, dict] = {}
        for r in rows:
            g = r.get(group) or "unknown"
            bucket = out.setdefault(
                g,
                {group: g, "calls": 0, **{k: 0 for k in self._TOKEN_KEYS}},
            )
            key = name_to_key.get(r.get("metric", ""))
            if key:
                bucket[key] = int(r.get("tokens", 0) or 0)
            # calls is emitted per metric row; take it from the Total Tokens row.
            if r.get("metric") == "Total Tokens":
                bucket["calls"] = int(r.get("calls", 0) or 0)
        return sorted(out.values(), key=lambda d: d.get("total", 0), reverse=True)

    def _backend_breakdown(
        self, subscription_ids: list[str] | None, hours: int
    ) -> list[dict]:
        """Per-hub token breakdown from the usage `trace` (App Insights `traces`).

        The real hub is only knowable from our trace (decoded session-affinity
        cookie), not from customMetrics. Each trace row carries the raw provider
        `usage` JSON, which we parse into the five token types. Grouped by hub.

        Caveat vs the customMetrics path: streaming (SSE) calls can't read the
        response body, so their trace usage is "BODY_READ_FAILED" and contributes
        0 tokens here (but still counts as a call). Non-streaming calls are exact.
        Rows shaped {"backend", total/prompt/cached/completion/reasoning, calls}.
        """
        # Restrict to a tenant's keys via the `subscription` trace dimension.
        sub_filter = ""
        if subscription_ids is not None:
            if not subscription_ids:
                return []
            quoted = ", ".join(f'"{s}"' for s in subscription_ids)
            sub_filter = (
                "| where tostring(customDimensions['subscription']) "
                f"in ({quoted}) "
            )
        # Pull one row per call with hub + the raw usage JSON string; parse in
        # Python (the usage shape varies by provider). Cap rows defensively.
        kql = (
            'traces | where message startswith "llm-usage " '
            + sub_filter
            + "| extend hub = tostring(customDimensions['hub']), "
            "usage = tostring(customDimensions['usage']) "
            "| project hub, usage "
            "| take 100000"
        )
        rows = self._run_kql(kql, hours)
        out: dict[str, dict] = {}
        for r in rows:
            hub = r.get("hub") or "unknown"
            bucket = out.setdefault(
                hub,
                {"backend": hub, "calls": 0, **{k: 0 for k in self._TOKEN_KEYS}},
            )
            bucket["calls"] += 1
            tok = _parse_usage_tokens(r.get("usage"))
            # tok includes the correctly-computed per-provider `total` (see
            # _parse_usage_tokens), so accumulating every key is enough — no
            # separate total math here.
            for k, v in tok.items():
                bucket[k] += v
        return sorted(out.values(), key=lambda d: d.get("total", 0), reverse=True)

    def token_usage_trend(
        self,
        subscription_ids: list[str] | None = None,
        hours: int = 24,
        bucket_minutes: int = 60,
    ) -> list[dict]:
        """Token + call time series (zero-filled) for the dual-line trend chart.

        BOTH series come from the SAME customMetrics rows so they're perfectly
        aligned on the same buckets and share the same subscription filter:
          * tokens = sum(valueSum)   — total tokens per bucket
          * calls  = sum(valueCount) — number of metered calls per bucket
            (valueCount is App Insights' measurement count; verified on dev-a03 to
            equal the metered call count — i.e. calls that produced a usage record)
        make-series zero-fills empty buckets so the timeline is continuous.
        Returns [{"ts", "tokens", "calls"}] oldest→newest.
        """
        if self._client is None or not self._resource_id:
            return []
        step = f"{bucket_minutes}m"
        kql = (
            "customMetrics "
            '| where name == "Total Tokens" '
            + self._sub_filter(subscription_ids)
            + "| make-series tokens = sum(valueSum) default = 0, "
            "calls = sum(valueCount) default = 0 "
            f"on timestamp from ago({hours}h) to now() step {step} "
            "| mv-expand timestamp to typeof(datetime), "
            "tokens to typeof(long), calls to typeof(long) "
            "| order by timestamp asc"
        )
        return [
            {
                "ts": str(r.get("timestamp")),
                "tokens": int(r.get("tokens", 0) or 0),
                "calls": int(r.get("calls", 0) or 0),
            }
            for r in self._run_kql(kql, hours)
        ]
