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
from datetime import timedelta

from azure.cosmos import CosmosClient, PartitionKey
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from app.config import get_settings
from app.models.schemas import UsageRecord

logger = logging.getLogger(__name__)


def _parse_usage_tokens(usage_json: str | None) -> tuple[int, int, int, int, int]:
    """Parse a raw provider `usage` JSON string (from the usage trace) into
    (prompt, completion, cached, creation, reasoning) token counts.

    Provider-agnostic — handles the field names all providers use:
      * Anthropic: input_tokens / output_tokens / cache_read_input_tokens /
        cache_creation_input_tokens
      * OpenAI/Google chat: prompt_tokens / completion_tokens /
        prompt_tokens_details.cached_tokens / completion_tokens_details.
        reasoning_tokens
    Non-JSON / "BODY_READ_FAILED" (streaming) → all zeros. `prompt` here is the
    NON-cached input for OpenAI-style (prompt_tokens already includes cached) vs
    Anthropic's input_tokens (excludes cache); we return the base input and the
    cache separately so the caller can present them without double counting.
    """
    if not usage_json or usage_json in ("BODY_READ_FAILED", "NO_USAGE_KEY"):
        return (0, 0, 0, 0, 0)
    try:
        u = json.loads(usage_json)
    except (ValueError, TypeError):
        return (0, 0, 0, 0, 0)
    if not isinstance(u, dict):
        return (0, 0, 0, 0, 0)

    def _i(v: object) -> int:
        try:
            return int(v)  # type: ignore[call-overload]
        except (ValueError, TypeError):
            return 0

    prompt = _i(u.get("input_tokens", u.get("prompt_tokens", 0)))
    completion = _i(u.get("output_tokens", u.get("completion_tokens", 0)))
    cached = _i(
        u.get("cache_read_input_tokens")
        or (u.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    creation = _i(u.get("cache_creation_input_tokens", 0))
    reasoning = _i(
        # Google puts thinking tokens at the TOP level as reasoning_tokens (with
        # completion_tokens=0 — the whole output is reasoning). OpenAI nests it in
        # completion_tokens_details.reasoning_tokens (a SUBSET of completion).
        u.get("reasoning_tokens")
        or (u.get("completion_tokens_details") or {}).get("reasoning_tokens")
        or (u.get("output_tokens_details") or {}).get("reasoning_tokens")
        or 0
    )
    # Google reports the visible output under reasoning_tokens with
    # completion_tokens=0, so its "output" is really the reasoning. Fold it into
    # completion when completion is 0 but reasoning is present, so downstream
    # total (= prompt + completion) matches the provider's total_tokens and the
    # output column isn't misleadingly empty. For OpenAI, reasoning is already
    # inside completion, so we DON'T add it again.
    if completion == 0 and reasoning > 0:
        completion = reasoning
    return (prompt, completion, cached, creation, reasoning)


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
        self, subscription_ids: list[str], limit: int = 1000, skip: int = 0
    ) -> list[dict]:
        """Usage records whose `subscription` (virtual key id) is in the given
        set. This is how a tenant's usage is resolved: the caller maps tenant ->
        its virtual keys via PostgreSQL, then we match those keys in Cosmos.
        Records are written with tenant='unknown' (no tenant header on the data
        plane yet), so the virtual key is the reliable tenant linkage.

        `skip`/`limit` give server-side pagination (OFFSET/LIMIT) for the portal
        call log; pair with count_by_subscriptions for total pages."""
        if not self._endpoint or not subscription_ids:
            return []
        # Cosmos supports ARRAY_CONTAINS(@ids, c.subscription) for an IN-style
        # filter against a parameterized list.
        return list(
            self._container.query_items(
                query=(
                    "SELECT * FROM c WHERE ARRAY_CONTAINS(@ids, c.subscription) "
                    "ORDER BY c.ts DESC OFFSET @skip LIMIT @n"
                ),
                parameters=[
                    {"name": "@ids", "value": subscription_ids},
                    {"name": "@skip", "value": skip},
                    {"name": "@n", "value": limit},
                ],
                enable_cross_partition_query=True,
            )
        )

    def count_by_subscriptions(self, subscription_ids: list[str]) -> int:
        """Total number of usage records for the given virtual keys — used to
        compute page count for the paginated call log. Returns 0 when Cosmos is
        not configured or the key set is empty."""
        if not self._endpoint or not subscription_ids:
            return 0
        rows = list(
            self._container.query_items(
                query=(
                    "SELECT VALUE COUNT(1) FROM c "
                    "WHERE ARRAY_CONTAINS(@ids, c.subscription)"
                ),
                parameters=[{"name": "@ids", "value": subscription_ids}],
                enable_cross_partition_query=True,
            )
        )
        return int(rows[0]) if rows else 0


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
          * trend — calls per hour (time trend)
        Each query degrades independently: if the fragile dependency join yields
        nothing, the base latency table still renders and the split columns show
        as null. App Insights telemetry is best-effort, separate from Cosmos
        usage which is the billing source.
        """
        empty: dict = {"by_api": [], "total_calls": 0, "by_hour": []}
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

        # 3) Trend: calls per hour, oldest→newest. make-series zero-fills the
        #    gaps so the chart shows a continuous 24h timeline (a plain summarize
        #    by bin() only emits hours that had calls — producing a few isolated
        #    spikes with empty space between, not a real time series).
        trend_kql = """
        requests
        | where name startswith 'POST /llm-'
        | make-series calls = count() default = 0
            on timestamp from ago(24h) to now() step 1h
        | mv-expand timestamp to typeof(datetime), calls to typeof(long)
        | order by timestamp asc
        """
        by_hour = [
            {"ts": str(r.get("timestamp")), "calls": int(r.get("calls", 0) or 0)}
            for r in self._run_kql(trend_kql, hours)
        ]

        total = sum(int(r.get("calls", 0) or 0) for r in by_api)
        return {"by_api": by_api, "total_calls": total, "by_hour": by_hour}

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
    # (verified live on dev-a03, StandardV2). Mapped to short, provider-neutral
    # keys the API/portal use. `value`/`valueSum` on customMetrics holds the token
    # count; we sum valueSum. Dimensions available: subscription (virtual key id),
    # api (llm-<provider>), model (request body's model, e.g. claude-opus-4.8).
    _TOKEN_METRICS = {
        "total": "Total Tokens",
        "prompt": "Prompt Tokens",
        "cached": "Prompt Cached Tokens",
        "completion": "Completion Tokens",
        "reasoning": "Completion Reasoning Tokens",
    }

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
                {group: g, "total": 0, "prompt": 0, "cached": 0,
                 "completion": 0, "reasoning": 0, "calls": 0},
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
                {"backend": hub, "total": 0, "prompt": 0, "cached": 0,
                 "completion": 0, "reasoning": 0, "calls": 0},
            )
            bucket["calls"] += 1
            prompt, comp, cached, creation, reason = _parse_usage_tokens(r.get("usage"))
            bucket["prompt"] += prompt
            bucket["cached"] += cached
            bucket["completion"] += comp
            bucket["reasoning"] += reason
            bucket["total"] += prompt + comp  # total = input + output (cache is subset)
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
