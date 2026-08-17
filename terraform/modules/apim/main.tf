# API Management — the GenAI gateway (data plane).
# Developer SKU for MVP; system-assigned identity used to reach AI backends and
# to publish custom token metrics to Application Insights.

terraform {
  required_providers {
    # azapi is used to patch the diagnostic `metrics` flag that azurerm doesn't
    # expose (required for llm-emit-token-metric to emit custom token metrics).
    azapi = {
      source = "Azure/azapi"
    }
  }
}

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }
variable "publisher_email" { type = string }
variable "publisher_name" { type = string }
variable "app_insights_id" { type = string }
variable "app_insights_connection_string" {
  type      = string
  sensitive = true
}
# APIM SKU. Default Developer_1 (classic, MVP/dev). Set to a v2 tier
# (e.g. "StandardV2_1", "BasicV2_1") for native Anthropic Messages API token
# metering — llm-emit-token-metric only understands the Anthropic response
# schema on v2 tiers (see docs/APIM-LLM-Gateway.md §4.6).
variable "sku_name" {
  type    = string
  default = "Developer_1"
}
# Log Analytics workspace that receives the APIM gateway LLM logs (below). This is
# Currently UNUSED — kept deliberately, do not "clean up".
#
# It fed the Azure Monitor diagnostic setting that shipped GatewayLogs to the
# workspace. That setting was removed (see the long note further down: the table
# duplicated AppRequests row for row and nothing read it). The variable stays so
# the restore snippet in that note is a single paste, and because the root
# module already wires it — removing it here means editing two files to bring
# gateway logging back for a debugging session.
variable "log_analytics_workspace_id" { type = string }

# The UPSTREAM half of App Insights sampling: how much telemetry APIM SENDS.
# The downstream half is the App Insights component's own ingestion sampling
# (modules/monitor). They sit in series on one pipe — but turn this one, not
# that one; see the root variable descriptions for why the downstream knob may
# have no effect at all on APIM-sourced telemetry.
#
# Written in TWO places below and they must not diverge — see the note on
# azapi_update_resource.diagnostic_metrics. That is exactly why this is a
# variable rather than a literal: the two writes now cannot disagree.
variable "sampling_percentage" {
  type    = number
  default = 100
}

resource "azurerm_api_management" "apim" {
  name                = substr("${var.name_prefix}-apim-${var.suffix}", 0, 50)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  publisher_email     = var.publisher_email
  publisher_name      = var.publisher_name
  # azurerm packs <tier>_<capacity>: Developer SKU capacity 1 by default; a v2
  # tier (StandardV2_1 / BasicV2_1) is passed in for native Anthropic metering.
  sku_name = var.sku_name

  identity {
    type = "SystemAssigned"
  }

  # Developer SKU has no zone redundancy. azurerm v4 otherwise tries to "change"
  # the computed `zones` field on every apply, which the API rejects (zone is
  # immutable post-create) and which aborts the run. Ignore it.
  lifecycle {
    ignore_changes = [zones]
  }
}

# Wire APIM telemetry into Application Insights (token metrics, request logs).
resource "azurerm_api_management_logger" "appinsights" {
  name                = "appinsights"
  api_management_name = azurerm_api_management.apim.name
  resource_group_name = var.resource_group_name
  resource_id         = var.app_insights_id

  application_insights {
    connection_string = var.app_insights_connection_string
  }
}

# Service-level diagnostic: this is what actually emits per-request telemetry
# (requests + backend dependencies) to the logger above. Without a diagnostic,
# APIM sends NEITHER request/latency logs NOR the custom token metrics.
#
# sampling_percentage 100 -> every request logged (right for MVP/debugging).
# Lower it (5-20) at scale to cut Log Analytics ingestion cost; latency
# percentiles stay accurate, you just lose the ability to find one specific
# request's trace.
#
# metrics = true is REQUIRED for llm-emit-token-metric to actually emit token
# custom metrics to App Insights (customMetrics table). Without it the diagnostic
# defaults metrics=null and every token metric is silently dropped — verified on
# dev-a02 (requests logged, customMetrics empty) before this was added.
resource "azurerm_api_management_diagnostic" "appinsights" {
  # Must be this exact identifier to bind to App Insights.
  identifier               = "applicationinsights"
  api_management_name      = azurerm_api_management.apim.name
  resource_group_name      = var.resource_group_name
  api_management_logger_id = azurerm_api_management_logger.appinsights.id

  sampling_percentage       = var.sampling_percentage
  always_log_errors         = true
  verbosity                 = "information"
  http_correlation_protocol = "W3C"
}

# azurerm_api_management_diagnostic does NOT expose the `metrics` flag, but
# llm-emit-token-metric REQUIRES it (verified on dev-a02: requests logged but
# customMetrics empty until metrics=true was PATCHed in). Patch it via azapi,
# preserving the settings azurerm wrote above.
#
# This resource depends on the azurerm one (via resource_id), so terraform
# always runs it SECOND — making it the last writer on the same ARM object.
# Every field it repeats therefore WINS. That was a trap while the percentage
# was a literal in both places: editing the azurerm one appeared to work,
# terraform reported success, the plan showed the change, and azapi silently put
# it back. Both now read `var.sampling_percentage`, so they cannot disagree.
#
# The repeated fields may not even be necessary — azapi_update_resource merges
# its body into the existing resource, so `metrics` alone might suffice. That is
# testable (drop them, apply, re-read sampling/alwaysLog from ARM) but untested,
# and whoever wrote them may have been working around something the comment does
# not record. Sharing the variable removes the hazard without betting on it.
resource "azapi_update_resource" "diagnostic_metrics" {
  type        = "Microsoft.ApiManagement/service/diagnostics@2022-08-01"
  resource_id = azurerm_api_management_diagnostic.appinsights.id
  body = {
    properties = {
      loggerId                = azurerm_api_management_logger.appinsights.id
      metrics                 = true
      alwaysLog               = "allErrors"
      verbosity               = "information"
      httpCorrelationProtocol = "W3C"
      sampling = {
        samplingType = "fixed"
        percentage   = var.sampling_percentage
      }
    }
  }
}

# NO Azure Monitor diagnostic setting on APIM — deliberately, and this is the
# second category dropped from it rather than an oversight.
#
# It used to route two categories to Log Analytics:
#
# 1. GatewayLlmLogs -> ApiManagementGatewayLlmLog. Dropped first, because its
#    token counts cannot be billed from. Measured on dev-15 (1180 requests):
#    the prompt-token basis varies BY PROVIDER — claude-opus-4.8 reported 1,381
#    against an actual 21,953 including cache reads (94% low), while
#    gpt-5.4-mini reported 19,169, exactly prompt+cached. Same table, opposite
#    conventions, and no column says which one a row uses. Streamed calls carry
#    no content at all (0 of 114 rows) because SSE has no response body for the
#    gateway to parse. The content it DID capture was a liability: full prompts
#    and completions for every non-streamed call, unconditionally and for every
#    tenant, which is not what docs/AUDIT.zh.md promises.
#
# 2. GatewayLogs -> ApiManagementGatewayLogs. Dropped now (2026-08-15). Nothing
#    reads it: a whole-repo search finds the string only in this comment. And it
#    is not merely unread, it is a DUPLICATE — measured on dev-19, that table
#    and AppRequests held exactly the same 2,053 rows at ~2 MB each, the same
#    calls written into the same workspace by two independent pipelines, one of
#    which nobody queries. Ingestion scales with call volume (~1 MB per 1,000
#    calls), so it is a bill that grows and never gets read.
#
# What still covers the ground it used to:
#   * per-call latency + status  -> App Insights `requests` (AppRequests)
#   * gateway vs backend split   -> `requests` joined to `dependencies`
#   * token counts               -> llm-emit-token-metric (customMetrics) and
#                                   the hub's copilot_usage via Event Hub -> Cosmos
#   * audit trail                -> the hub's audit blob, not a gateway log
#
# What is genuinely given up: client IP, cache-hit status, and the raw
# api/operation ids — useful for ad-hoc forensics, used by nothing today.
#
# Dropping the resource also removes a v2-tier constraint: GatewayLlmLogs does
# not exist on Developer_1, so this used to fail apply on the default SKU.
#
# To bring it back for a debugging session, add:
#   resource "azurerm_monitor_diagnostic_setting" "apim_gateway_logs" {
#     name                           = "apim-gateway-logs"
#     target_resource_id             = azurerm_api_management.apim.id
#     log_analytics_workspace_id     = var.log_analytics_workspace_id
#     log_analytics_destination_type = "Dedicated"
#     enabled_log { category = "GatewayLogs" }
#   }

# NOTE: APIM's identity deliberately has NO Cosmos role. It held "Cosmos DB Data
# Contributor" for an outbound policy that wrote one usage document per call.
# That policy is gone — usage now travels hub -> Event Hub -> Capture -> import
# job -> Cosmos, and the gateway never touches the billing store. The grant was
# standing write access to every tenant's billing data held by a component with
# no reason to reach it, so it is removed rather than left as "harmless".

# Let APIM's managed identity PUBLISH custom metrics to Application Insights.
# llm-emit-token-metric writes per-call token counts (incl. the Prompt Cached
# Tokens dimension) to the customMetrics table — but only if APIM's identity has
# "Monitoring Metrics Publisher" on the App Insights component. This is the real
# enabler for the customMetrics token breakdown (incl. cached), NOT the stuck
# subscription feature Microsoft.Insights/EnableCustomMetricsV2 (which was an
# early dead-end — see docs/APIM-LLM-Gateway.md §4). Without this role the
# customMetrics table stays empty (verified: dev-a05 with the role has data incl.
# cached; a fresh env without it is empty).
resource "azurerm_role_assignment" "apim_metrics_publisher" {
  scope                = var.app_insights_id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_api_management.apim.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}
