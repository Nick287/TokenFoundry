# Cosmos DB for NoSQL — high-write usage records.
# partition key = /pk (subscriptionId_yyyymm); raw records get a 90-day TTL.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

variable "throughput_rus" {
  description = <<-EOT
    Manual provisioned RU/s on the `usage` container. 0 = serverless (default).

    WHICH TO PICK looks like a utilisation question and is really a burstiness
    question. Provisioned works out to $0.0222 per million RU ($0.008/hour per
    100 RU/s, southeastasia) against serverless's $0.285 — 12.8x cheaper per RU
    — which suggests provisioned wins above ~8% utilisation, about 8.2M
    calls/month at the 400 RU/s minimum.

    dev-18 was built provisioned to test that, and it does not hold. 8.2M
    calls/month is ~32 RU/s averaged; the dev-18 campaign ran at 33 RU/s
    averaged and was throttled 2,904 times, pinned at 100% of the ceiling for 45
    minutes. The break-even volume is already past what 400 RU/s can serve.

    Peak demand across a whole minute was only 255 RU/s — under the limit — but
    Cosmos enforces per second and this workload is spiky by construction: the
    importer flushes an entire Capture blob in one go every 300s, and a single
    cross-partition aggregate can cost 5,000 RU, which is 12.5 seconds of a
    400 RU/s budget in ONE query. Serverless has no ceiling to hit.

    Nothing is lost when it throttles — the importer only advances its watermark
    once a whole blob is in, and the SDK retries 429 — so the price of choosing
    wrong is import latency, not a billing gap.

    WARNING: this is NOT a live switch, and terraform's plan does NOT say so.

    `EnableServerless` is an account-level capability fixed at creation —
    Azure's own words are "when you create an Azure Cosmos DB account, you
    choose between provisioned throughput and serverless". Worse, azurerm
    treats `capabilities` as computed, so REMOVING the block produces no diff
    at all: flipping this variable on a live serverless environment plans a
    single benign-looking "container updated in-place, + throughput = 400" and
    then FAILS at apply, because Azure rejects throughput on a serverless
    container. (Verified against dev-17: the account appears in the plan only
    as "Refreshing state", with zero planned changes.)

    The failure is safe — nothing is destroyed — but the switch cannot be made
    this way. Set this at CREATION time on a new environment, or migrate the
    documents to a new account.
  EOT
  type        = number
  default     = 0

  validation {
    # 400 RU/s is the Azure minimum for a container with manual throughput;
    # anything between 1 and 399 is silently invalid at apply time, which is a
    # worse place to find out.
    condition     = var.throughput_rus == 0 || var.throughput_rus >= 400
    error_message = "throughput_rus must be 0 (serverless) or at least 400 RU/s."
  }
}

resource "azurerm_cosmosdb_account" "account" {
  name                = substr("${var.name_prefix}-cosmos-${var.suffix}", 0, 44)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  offer_type          = "Standard"
  kind                = "GlobalDocumentDB"

  # No keys — data-plane access is AAD/RBAC only (mirrors Bicep
  # disableLocalAuth=true). The app + APIM identities get data-plane role
  # assignments in their respective modules. (azurerm v4 renamed the old
  # local_authentication_disabled=true to local_authentication_enabled=false.)
  local_authentication_enabled = false

  consistency_policy {
    consistency_level = "Session"
  }

  # Present only in serverless mode. The capability cannot be added to or
  # removed from a live account, so toggling `throughput_rus` forces a replace.
  dynamic "capabilities" {
    for_each = var.throughput_rus > 0 ? [] : [1]
    content {
      name = "EnableServerless"
    }
  }

  geo_location {
    location          = var.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "db" {
  name                = "tokenfoundry"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.account.name
}

resource "azurerm_cosmosdb_sql_container" "usage" {
  name                = "usage"
  resource_group_name = var.resource_group_name
  account_name        = azurerm_cosmosdb_account.account.name
  database_name       = azurerm_cosmosdb_sql_database.db.name
  partition_key_paths = ["/pk"]
  # 90-day TTL on raw records; aggregates are rolled up to PostgreSQL.
  default_ttl = 7776000
  # null in serverless mode — passing any throughput to a serverless container
  # is an error, not a no-op.
  throughput = var.throughput_rus > 0 ? var.throughput_rus : null
}
