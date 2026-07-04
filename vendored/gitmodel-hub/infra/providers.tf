terraform {
  required_version = ">= 1.5.0"

  # P2: remote state in an azurerm blob backend. Partial config — the concrete
  # storage_account_name / container_name / key are supplied at init time by the
  # deploy Job's entrypoint via `terraform init -backend-config=...` (key is
  # per-account: hubs/<account_id>.tfstate). This makes the Job stateless: any
  # execution can manage any account's hub by pointing init at the right key.
  # For local P1-style runs, `terraform init -backend=false` uses local state.
  backend "azurerm" {}

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 4.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }
}

provider "azurerm" {
  features {}
}
