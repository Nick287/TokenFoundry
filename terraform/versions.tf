# Token Foundry — Terraform provider requirements.
#
# Two providers, by design:
#   * azurerm — the strongly-typed mainline provider, used for ~90% of resources.
#   * azapi   — Microsoft's thin ARM REST wrapper, used ONLY for the APIM backend
#               pool + circuit breaker, which ride a PREVIEW API version
#               (2023-09-01-preview). azurerm has no native resource for that yet;
#               azapi calls the preview API directly, on par with Bicep. This is
#               what historically made the team pick Bicep over Terraform — azapi
#               removes that blocker.
#
# Exact versions are locked by the committed .terraform.lock.hcl (run
# `terraform init` to generate it, `terraform init -upgrade` to bump).

terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
    # time — used by the keyvault module to pause for RBAC propagation after
    # granting the deployer Secrets Officer, before secrets are written.
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }
}

# features{} is required even when empty.
provider "azurerm" {
  features {}
}

# azapi shares Azure CLI / environment-variable auth with azurerm.
provider "azapi" {}
