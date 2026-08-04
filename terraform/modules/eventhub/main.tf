# Event Hub — the usage-record transport between every GitModel hub and the
# control plane's billing store.
#
# Why a bus and not a direct write: hubs are per-account Container Apps that may
# live in other resource groups (方案 A deploys them from a GitHub Action under a
# separate SP). Letting each one write Cosmos directly would mean handing every
# hub a Cosmos data-plane role, and a Cosmos throttle or outage would push back
# onto the request path of a live gateway. Event Hub is the buffer: the hub's
# producer is fire-and-forget (see hub/eventhub.py) and a broker problem costs
# events, never requests.
#
# Why Capture instead of a consumer app: Capture is a built-in, zero-code drain
# to Blob (Avro), so the control plane needs no long-lived consumer process, no
# checkpoint store, and no partition/lease management — it just lists new blobs
# on a timer and imports them. The cost is latency: `capture_interval_seconds`
# is the floor on how stale billing data can be. That is fine for billing and
# explicitly NOT the path for real-time usage (that stays on App Insights).
#
# Capture requires a Standard (or higher) namespace — Basic does not support it.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

variable "capacity" {
  description = "Throughput Units. 1 TU = 1000 events/s ingress. Measured load is ~1 event/s per hub (upstream caps concurrency long before that), so 1 TU has ~3 orders of magnitude of headroom."
  type        = number
  default     = 1
}

variable "partition_count" {
  description = "Partitions on the usage hub. Capture writes one blob path per partition, so more partitions means more, smaller Avro files per interval."
  type        = number
  default     = 2
}

variable "message_retention_days" {
  description = "How long events stay replayable in the hub. This is the window in which a broken import job can be re-run without data loss, so it wants to be comfortably longer than the alerting delay."
  type        = number
  default     = 7
}

variable "capture_interval_seconds" {
  description = "Capture flushes a blob at whichever comes first — this interval or capture_size_limit_bytes. Also the floor on billing-data staleness. Azure allows 60-900."
  type        = number
  default     = 300
}

variable "capture_size_limit_bytes" {
  description = "Size trigger for a Capture flush. Azure allows 10 MB - 500 MB."
  type        = number
  default     = 104857600 # 100 MB
}

variable "capture_retention_days" {
  description = "Days a Capture blob is kept before lifecycle management deletes it. Must exceed message_retention_days — past that point a lost blob is unrecoverable from the hub too."
  type        = number
  default     = 30
}

resource "azurerm_eventhub_namespace" "ns" {
  name                = substr("${var.name_prefix}-ehns-${var.suffix}", 0, 50)
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  # Standard is the minimum tier that supports Capture.
  sku      = "Standard"
  capacity = var.capacity

  # No SAS connection strings anywhere: hubs authenticate with their managed
  # identity (Azure Event Hubs Data Sender, granted in the hub's own terraform),
  # so there is no shared secret to leak or rotate.
  local_authentication_enabled = false
}

resource "azurerm_storage_account" "capture" {
  # Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
  name                     = substr("${var.name_prefix}usage${var.suffix}", 0, 24)
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags
  # Shared key stays enabled because the azurerm provider creates the container
  # below over the storage data plane. It is NOT how Capture authenticates:
  # Microsoft.EventHub is a trusted first-party service and writes to a
  # firewall-free storage account without any role assignment or key of ours.
  # (If a firewall is ever added here, Capture will need a namespace managed
  # identity + Storage Blob Data Contributor — it silently stops otherwise.)
  # Our own read path is AAD only: the control plane's identity gets Storage
  # Blob Data Reader, granted in the containerapps module.
  shared_access_key_enabled       = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"
}

resource "azurerm_storage_container" "capture" {
  name                  = "usage-capture"
  storage_account_id    = azurerm_storage_account.capture.id
  container_access_type = "private"
}

resource "azurerm_eventhub" "usage" {
  name              = "usage"
  namespace_id      = azurerm_eventhub_namespace.ns.id
  partition_count   = var.partition_count
  message_retention = var.message_retention_days

  capture_description {
    enabled  = true
    encoding = "Avro"
    # Whichever fires first wins.
    interval_in_seconds = var.capture_interval_seconds
    size_limit_in_bytes = var.capture_size_limit_bytes
    # Suppress the empty blob Capture would otherwise write for an interval
    # with no events: pure cost and noise for the import job's blob listing.
    skip_empty_archives = true

    destination {
      name = "EventHubArchive.AzureBlockBlob"
      # NOTE the ordering: {PartitionId} sits ABOVE the date segments, so blob
      # names do NOT sort chronologically across partitions — a fresh partition-0
      # blob sorts before an older partition-1 one. That is why the import job
      # keeps a `last_modified` watermark rather than a name prefix; see
      # app/services/usage_capture_import.py.
      archive_name_format = "{Namespace}/{EventHub}/{PartitionId}/{Year}/{Month}/{Day}/{Hour}/{Minute}/{Second}"
      blob_container_name = azurerm_storage_container.capture.name
      storage_account_id  = azurerm_storage_account.capture.id
    }
  }
}

# Capture blobs are a transport, not an archive: once the import job has turned
# one into Cosmos documents it has no further use. Nothing deletes them
# otherwise, and the import job LISTS the container every pass — so unbounded
# growth costs storage AND makes every run slower forever.
#
# The retention has to clear two bars: longer than the Event Hub's own
# message_retention_days (past that, a lost blob can't be replayed from the hub
# either), and long enough that a multi-day import outage is still recoverable.
# 30 days over a 7-day hub retention gives three weeks of slack.
resource "azurerm_storage_management_policy" "capture_retention" {
  storage_account_id = azurerm_storage_account.capture.id

  rule {
    name    = "expire-imported-capture-blobs"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.capture.name}/"]
    }

    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.capture_retention_days
      }
    }
  }
}
