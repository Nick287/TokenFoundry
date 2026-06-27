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
        query = (
            "SELECT * FROM c WHERE c.tenant_id = @t "
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


class AppInsightsUsage:
    """Phase 1 KQL pull of llm-emit-token-metric custom metrics."""

    def __init__(self) -> None:
        self._resource_id = get_settings().app_insights_resource_id
        self._client = LogsQueryClient(credential=DefaultAzureCredential())

    def token_usage_by_tenant(self, days: int = 1) -> list[dict]:
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
