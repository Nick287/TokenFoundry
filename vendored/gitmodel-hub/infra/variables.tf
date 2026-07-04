variable "resource_group_name" {
  type        = string
  description = "Resource group to create for the Container App resources."
  default     = "gitmodel-rg"
}

variable "location" {
  type        = string
  description = "Azure region for all resources."
  default     = "centralus"
}

variable "prefix" {
  type        = string
  description = "Short name prefix used for resource names (lowercase, no spaces)."
  default     = "gitmodel"
}

# --- Container registry (create if missing, else use existing) ------------

variable "create_acr" {
  type        = bool
  description = "true (default): create a new Azure Container Registry in the app's resource group with a unique name (prefix + random suffix). false: use an existing registry given by acr_name / acr_resource_group_name. Terraform can't auto-detect existence, so set this to match reality."
  default     = true
}

variable "acr_name" {
  type        = string
  description = "Name of an EXISTING Azure Container Registry to build into and pull from. Only used when create_acr = false."
  default     = "azureaidemoacr"
}

variable "acr_resource_group_name" {
  type        = string
  description = "Resource group of the existing Azure Container Registry. Only used when create_acr = false."
  default     = "rg-azureaidemo"
}

# --- Container image (built in-cloud by `az acr build`) -------------------

variable "image_name" {
  type        = string
  description = "Image repository name inside the registry. The tag is derived automatically from a hash of the source (hub/, Dockerfile, requirements.txt)."
  default     = "gitmodel"
}

variable "image_ref_override" {
  type        = string
  description = "P2: when set (e.g. \"gitmodel:v1\"), reference this pre-built image verbatim and SKIP the per-account az acr build. Combined with create_acr=false + acr_name pointing at the shared registry, the Container App pulls <shared-acr>/<override>. Empty (default) keeps the P1 behavior of building a per-source-hash tag."
  default     = ""
}

# --- Persistent storage --------------------------------------------------
# UNUSED as of the stateless-hub refactor: the per-account Azure Files storage
# account + share were removed (the hub keeps no durable state; SQLite is an
# ephemeral scratch DB under /tmp). Kept as a no-op var so any external tfvars
# that still set it don't error. Safe to delete once no caller references it.

variable "file_share_name" {
  type        = string
  description = "DEPRECATED / UNUSED — kept only for tfvars back-compat after storage removal."
  default     = "gitmodel-data"
}

# --- App runtime settings -------------------------------------------------

variable "copilot_oauth_token" {
  type        = string
  description = "GitHub Copilot OAuth token for the account this hub instance serves. Injected as the COPILOT_OAUTH_TOKEN env var (via a Container App secret) so the hub is authenticated without going through the web portal's device flow. Leave empty to authenticate later via the portal."
  default     = ""
  sensitive   = true
}

variable "container_port" {
  type        = number
  description = "Port the app listens on inside the container."
  default     = 8088
}

variable "require_auth" {
  type        = bool
  description = "Set HUB_REQUIRE_AUTH. Keep true when exposed to the public internet."
  default     = true
}

variable "hub_admin_token" {
  type        = string
  description = "Deploy-time HUB_ADMIN_TOKEN — an env-override admin token the control plane uses to call the management API (POST /api/keys) without the portal login. Injected as a Container App secret. Empty => portal login only."
  default     = ""
  sensitive   = true
}

variable "hub_api_key" {
  type        = string
  description = "Deploy-time HUB_API_KEY accepted for /v1/* auth (control-plane managed, Key Vault-backed, injected as a Container App secret). The stateless hub keeps no durable keys, so this env key is the credential APIM authenticates with. Empty => only portal-created keys work (non-persistent)."
  default     = ""
  sensitive   = true
}

variable "login_max_fails" {
  type        = number
  description = "HUB_LOGIN_MAX_FAILS: consecutive failed admin logins from one IP before lockout. Set 0 to disable throttling."
  default     = 5
}

variable "login_lock_seconds" {
  type        = number
  description = "HUB_LOGIN_LOCK_SECONDS: how long (seconds) an IP stays locked after too many failed admin logins."
  default     = 900
}

variable "cpu" {
  type        = number
  description = "vCPU per replica."
  default     = 0.5
}

variable "memory" {
  type        = string
  description = "Memory per replica (must pair with cpu, e.g. 1.0Gi for 0.5 vCPU)."
  default     = "1.0Gi"
}
