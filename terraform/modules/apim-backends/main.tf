# APIM backend pool + circuit breaker — the GenAI gateway resilience features.
#
# This is the ONE module that cannot use azurerm: backend pools + circuit
# breakers ride the PREVIEW API version (2023-09-01-preview). The azapi provider
# calls that preview API directly, exactly like Bicep does — which is what
# historically made the team pick Bicep over Terraform. azapi removes the
# blocker, so the rest of the stack can be plain azurerm.
#
# This module shows the canonical shape; real per-tenant/per-provider backends
# are added at runtime by the FastAPI provisioner.

terraform {
  required_providers {
    azapi = {
      source = "Azure/azapi"
    }
  }
}

variable "apim_id" { type = string }
variable "backend_urls" { type = list(string) }

# Single backends with circuit breaker rules (trip on 5xx, honor Retry-After).
# for_each mirrors the Bicep `[for (url, i) in backendUrls]` loop.
resource "azapi_resource" "single" {
  for_each = { for i, url in var.backend_urls : tostring(i) => url }

  type      = "Microsoft.ApiManagement/service/backends@2023-09-01-preview"
  parent_id = var.apim_id
  name      = "pooled-backend-${each.key}"

  body = {
    properties = {
      url      = each.value
      protocol = "http"
      circuitBreaker = {
        rules = [
          {
            name = "trip-on-5xx"
            failureCondition = {
              count    = 3
              interval = "PT1H"
              statusCodeRanges = [
                {
                  min = 500
                  max = 599
                }
              ]
              errorReasons = ["Server errors"]
            }
            tripDuration     = "PT1H"
            acceptRetryAfter = true
          }
        ]
      }
    }
  }

  # Preview schema can lag the live API; skip client-side validation.
  schema_validation_enabled = false
}

# Load-balanced pool across the single backends (round-robin by default).
resource "azapi_resource" "pool" {
  type      = "Microsoft.ApiManagement/service/backends@2023-09-01-preview"
  parent_id = var.apim_id
  name      = "llm-pool"

  body = {
    properties = {
      description = "Load-balanced pool for pooled LLM backends"
      type        = "Pool"
      pool = {
        services = [
          for i, url in var.backend_urls : {
            id       = "${var.apim_id}/backends/pooled-backend-${i}"
            priority = 1
            weight   = 1
          }
        ]
      }
    }
  }

  schema_validation_enabled = false
  # Mirrors the Bicep dependsOn: pool references the singles by id.
  depends_on = [azapi_resource.single]
}
