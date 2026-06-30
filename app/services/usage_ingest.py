"""Usage ingest + read.

Phase 1: pull token metrics from Application Insights via KQL (azure-monitor-
query), aggregate, and persist raw records to Cosmos DB for NoSQL.
Phase 2: switch the source of truth to an Event Hub consumer (worker/) for
billing-grade, replayable accounting.

This module owns the Cosmos write path and the KQL read path. Billing math is
delegated to app.services.billing.
"""

from __future__ import annotations

import logging
from datetime import timedelta

from azure.cosmos import CosmosClient, PartitionKey
from azure.identity import DefaultAzureCredential
from azure.monitor.query import LogsQueryClient, LogsQueryStatus

from app.config import get_settings
from app.models.schemas import UsageRecord

logger = logging.getLogger(__name__)


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

    def token_usage_by_tenant(self, days: int = 1) -> list[dict]:
        if self._client is None:
            return []
        kql = """
        customMetrics
        | where name == 'llm-total-tokens'
        | extend tenant = tostring(customDimensions['tenant'])
        | extend route = tostring(customDimensions['route'])
        | summarize tokens = sum(value) by tenant, route
        """
        response = self._client.query_resource(
            self._resource_id, kql, timespan=timedelta(days=days)
        )
        if response.status != LogsQueryStatus.SUCCESS or not response.tables:
            logger.warning("KQL usage query returned no data: %s", response.status)
            return []
        table = response.tables[0]
        return [dict(zip(table.columns, row, strict=False)) for row in table.rows]
