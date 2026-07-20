#!/usr/bin/env python3
"""a12 fresh-deploy verification: customMetrics (incl cached) + LlmLog both alive."""
import datetime
from azure.monitor.query import LogsQueryClient
from azure.identity import DefaultAzureCredential

WS = "e7d5c7cb-f2df-479f-b6d0-0a9f2a91c199"
c = LogsQueryClient(credential=DefaultAzureCredential())

m = c.query_workspace(
    WS,
    'AppMetrics | where TimeGenerated > ago(15m) | summarize v=sum(Sum) by Name',
    timespan=datetime.timedelta(minutes=15),
)
metrics = {row[0]: row[1] for t in m.tables for row in t.rows}

lg = c.query_workspace(
    WS,
    'ApiManagementGatewayLlmLog | where TimeGenerated > ago(15m) | summarize n=count(), prompt=sum(PromptTokens)',
    timespan=datetime.timedelta(minutes=15),
)
llm = [list(row) for t in lg.tables for row in t.rows]

cm_ok = bool(metrics)
llm_ok = bool(llm and llm[0][0] and llm[0][0] > 0)
cached = metrics.get("Prompt Cached Tokens", "n/a")
print(f"customMetrics={'YES' if cm_ok else 'no'}(cached={cached}) | LlmLog={'YES' if llm_ok else 'no'}({llm[0] if llm else '-'})")
if cm_ok and llm_ok:
    print("BOTH_ALIVE")
