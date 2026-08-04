# Audit archive — raw request/response bodies for tenants that opted in.
#
# A SEPARATE storage account from Event Hub Capture, on purpose. Capture blobs
# are billing telemetry: token counts and prices, safe for the control plane to
# read across every tenant. What lands HERE is customer content — source code,
# and in practice whatever someone pasted into a prompt, which includes secrets
# and personal data. Those two things want different retention, different
# auditing, and above all different access lists, and the only reliable way to
# keep them different is to not put them in the same account.
#
# Access model, deliberately narrow:
#   * Each hub's managed identity gets Storage Blob Data Contributor scoped to
#     the CONTAINER (not the account). The grant is made in the hub's own
#     terraform — hubs live in per-account resource groups deployed by a separate
#     SP, so their principal ids do not exist here. This module only exports the
#     scope; see vendored/gitmodel-hub/infra/main.tf. Azure has no write-only
#     blob role, so Contributor is the least-privilege role that can create a
#     blob.
#   * The control plane gets NOTHING here. It records the blob path next to the
#     usage document so an operator can find a payload, but READING one requires
#     a role granted deliberately, out of band, to a named human or group. The
#     portal becoming able to render customer prompts should never be a side
#     effect of a terraform apply.
#
# Blob layout is YYYY/MM/DD/<subscription>/<request_id>.json.gz (hub/audit.py),
# so both retention and a per-tenant export are prefix operations.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }
variable "suffix" { type = string }

variable "retention_days" {
  description = "Days an audit payload is kept before lifecycle management deletes it. A compliance parameter, not a cost knob: it answers 'how far back can you show me what was actually sent', and it is equally the window in which a customer's content still exists after they ask for it to be gone."
  type        = number
  default     = 90
}

variable "soft_delete_days" {
  description = "Grace period after a delete. Guards against an over-broad lifecycle rule or a mistaken manual purge destroying exactly the records an audit is asking for."
  type        = number
  default     = 7
}

resource "azurerm_storage_account" "audit" {
  # Storage account names: globally unique, 3-24 chars, lowercase alphanumeric.
  name                     = substr("${var.name_prefix}audit${var.suffix}", 0, 24)
  resource_group_name      = var.resource_group_name
  location                 = var.location
  account_tier             = "Standard"
  account_replication_type = "LRS"
  tags                     = var.tags

  # Shared key stays enabled ONLY because the azurerm provider creates the
  # container below over the storage data plane (same constraint as the capture
  # account). Nothing in the running system uses it: hubs write with their
  # managed identity. If the provider ever creates containers over ARM, flip
  # this to false — an account key here is a key to every archived prompt.
  shared_access_key_enabled       = true
  allow_nested_items_to_be_public = false
  min_tls_version                 = "TLS1_2"

  blob_properties {
    delete_retention_policy {
      days = var.soft_delete_days
    }
  }
}

resource "azurerm_storage_container" "audit" {
  name                  = "audit"
  storage_account_id    = azurerm_storage_account.audit.id
  container_access_type = "private"
}

# Retention is enforced by the platform rather than a cleanup job, so it keeps
# running even if every service that writes here is down or deleted. A cleanup
# job that stops silently turns a 90-day promise into an indefinite archive of
# customer source code.
resource "azurerm_storage_management_policy" "retention" {
  storage_account_id = azurerm_storage_account.audit.id

  rule {
    name    = "expire-audit-payloads"
    enabled = true

    filters {
      blob_types   = ["blockBlob"]
      prefix_match = ["${azurerm_storage_container.audit.name}/"]
    }

    actions {
      base_blob {
        delete_after_days_since_creation_greater_than = var.retention_days
      }
    }
  }
}
