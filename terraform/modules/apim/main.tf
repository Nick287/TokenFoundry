# API Management — the GenAI gateway (data plane).
# Developer SKU for MVP; system-assigned identity used to reach AI backends and
# to be granted Cosmos data-plane write on the usage container.

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
# Cosmos DB — APIM writes usage records directly (outbound policy, MI auth).
variable "cosmos_account_name" { type = string }
variable "cosmos_account_id" { type = string }
# APIM SKU. Default Developer_1 (classic, MVP/dev). Set to a v2 tier
# (e.g. "StandardV2_1", "BasicV2_1") for native Anthropic Messages API token
# metering — llm-emit-token-metric only understands the Anthropic response
# schema on v2 tiers (see docs/APIM-LLM-Gateway.md §4.6).
variable "sku_name" {
  type    = string
  default = "Developer_1"
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

  sampling_percentage       = 100
  always_log_errors         = true
  verbosity                 = "information"
  http_correlation_protocol = "W3C"
}

# azurerm_api_management_diagnostic does NOT expose the `metrics` flag, but
# llm-emit-token-metric REQUIRES it (verified on dev-a02: requests logged but
# customMetrics empty until metrics=true was PATCHed in). Patch it via azapi,
# preserving the settings azurerm wrote above.
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
        percentage   = 100
      }
    }
  }
}

# Grant APIM's system identity Cosmos DB data-plane write access. The outbound
# policy uses this identity to write a usage record per LLM call directly to the
# `usage` container via the Cosmos REST API. The account sets
# local_authentication_enabled=false, so this data-plane RBAC assignment is
# required — control-plane roles do NOT grant it. Built-in "Cosmos DB Data
# Contributor" (...0002) covers item create/upsert.
resource "azurerm_cosmosdb_sql_role_assignment" "apim_cosmos_writer" {
  resource_group_name = var.resource_group_name
  account_name        = var.cosmos_account_name
  role_definition_id  = "${var.cosmos_account_id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
  principal_id        = azurerm_api_management.apim.identity[0].principal_id
  scope               = var.cosmos_account_id
}
