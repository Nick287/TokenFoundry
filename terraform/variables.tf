# Token Foundry — root input variables.
# Mirrors the params in infra/main.bicep / infra/main.bicepparam.
# Secrets (no default) should be passed via TF_VAR_* env vars, never committed.

variable "name_prefix" {
  description = "Short name prefix for all resources, e.g. \"tokenfoundry\""
  type        = string
  default     = "tokenfoundry"
}

variable "location" {
  description = "Azure region for all resources. centralus: some resources (e.g. PostgreSQL) are restricted from eastus."
  type        = string
  default     = "centralus"
}

variable "environment_name" {
  description = "Environment tag: dev | prod"
  type        = string
  default     = "dev"
}

variable "resource_group_name" {
  description = "Resource group to create and deploy into (Bicep assumed it pre-existed; Terraform creates it)."
  type        = string
  default     = "tokenfoundry-rg"
}

variable "pg_admin_login" {
  description = "PostgreSQL admin login"
  type        = string
  default     = "tfadmin"
}

variable "pg_admin_password" {
  description = "PostgreSQL admin password. Pass via TF_VAR_pg_admin_password."
  type        = string
  sensitive   = true
}

variable "jwt_secret" {
  description = "HS256 signing secret for self-hosted login JWTs. Pass via TF_VAR_jwt_secret."
  type        = string
  sensitive   = true
}

variable "admin_password" {
  description = "Seed admin account password. Pass via TF_VAR_admin_password."
  type        = string
  sensitive   = true
}

# Two tags, not one, because the two images are built by different scripts at
# different times: deploy.sh builds BOTH tokenfoundry:<tag> and gitmodel:<tag>,
# while update-app.sh rebuilds only the app. Driving both from a single variable
# meant no value was ever correct once they diverged — pointing it at the newest
# app tag named a gitmodel image that had never been built.
#
# Neither has a default. The old default was "latest", which reads like "newest"
# but is just an ordinary tag name that nothing in this repo ever pushes:
#
#   $ az acr manifest show -r <acr> -n tokenfoundry:latest
#   ERROR: manifest tagged by "latest" is not found.
#
# So a bare `terraform apply` silently assembled an image ref that could not be
# pulled. Failing with "No value for required variable" is the better outcome.
variable "image_tag" {
  description = "Tag of the app image in ACR (deploy.sh / update-app.sh build & push tokenfoundry:<tag>). The Container App image ref is assembled as <acr-login-server>/tokenfoundry:<tag>. Only applied when the app is FIRST created — see the lifecycle block in modules/containerapps."
  type        = string

  validation {
    condition     = var.image_tag != "latest"
    error_message = "This repo never pushes a 'latest' tag, so it resolves to no image. Pass the timestamped tag deploy.sh printed, e.g. v20260806142705."
  }
}

variable "hub_image_tag" {
  description = "Tag of the GitModel hub image in ACR (gitmodel:<tag>), published to the Container App as TF_HUB_IMAGE_TAG so hub deploys pull an image that exists. Changes only when deploy.sh rebuilds the hub, which is also the only time it runs terraform."
  type        = string

  validation {
    condition     = var.hub_image_tag != "latest"
    error_message = "This repo never pushes a 'latest' tag, so it resolves to no image. Pass the timestamped tag deploy.sh printed for gitmodel."
  }
}

variable "publisher_email" {
  description = "Publisher email for APIM"
  type        = string
  default     = "admin@tokenfoundry.local"
}

variable "publisher_name" {
  description = "Publisher org name for APIM"
  type        = string
  default     = "Token Foundry"
}

variable "apim_sku" {
  description = "APIM SKU. Default Developer_1 (classic, dev). Set a v2 tier (StandardV2_1 / BasicV2_1) for native Anthropic Messages API token metering (v2-only)."
  type        = string
  default     = "Developer_1"
}

# --- GitHub repo hosting deploy-hub.yml (方案 A hub deploys) ---
# The control plane pushes the deployer SP creds into THIS repo's Actions
# secrets and triggers its deploy-hub.yml. Override in terraform.tfvars to
# point a different fork/org at the same control plane.
variable "github_repo_owner" {
  description = "Owner (user/org) of the repo hosting deploy-hub.yml."
  type        = string
  default     = "Nick287"
}

variable "github_repo_name" {
  description = "Name of the repo hosting deploy-hub.yml."
  type        = string
  default     = "TokenFoundry"
}
