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

variable "image_tag" {
  description = "Tag of the app image in ACR (the deploy script builds & pushes tokenfoundry:<tag>). The Container App image ref is assembled as <acr-login-server>/tokenfoundry:<tag>."
  type        = string
  default     = "latest"
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

variable "backend_urls" {
  description = "Example backend endpoints to pool in APIM. Replace/extend at runtime via the provisioner."
  type        = list(string)
  default = [
    "https://example-aoai-1.openai.azure.com/openai",
    "https://example-aoai-2.openai.azure.com/openai",
  ]
}
