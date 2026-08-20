# Log Analytics workspace + Application Insights — Azure Monitor foundation.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }

# How long Log Analytics keeps data, in days.
#
# NOT a cost lever: PerGB2018 includes the first 31 days of retention
# free, so anything at or under that bills the same as 7. What you pay
# for is INGESTION — each GB is charged once on the way in. Shortening
# retention only shortens how far back you can look.
#
# It IS a data-minimisation lever: the diagnostic has logClientIp on, so
# these tables hold caller IP addresses. Keeping them 7 days instead of
# 30 is a smaller footprint of personal data, which is a real reason even
# though it is not a financial one.
#
# ⚠️ Azure may reject values below 30 on PerGB2018 — 7 is documented as
# available only on the legacy Free tier. Verified on dev-19 (see the
# apply result recorded with this change).
variable "retention_in_days" {
  type    = number
  default = 30
}

# The DOWNSTREAM half of App Insights sampling: how much the component KEEPS of
# what reaches it. The upstream half is the APIM diagnostic's own sampling
# (modules/apim), which decides how much is sent in the first place.
#
# Prefer the upstream knob. This one may be inert for APIM telemetry: Azure
# disables ingestion sampling for a type that already has fixed-rate sampling,
# and the APIM diagnostic always sends samplingType "fixed". Unverified — every
# measurement here was taken with both at 100.
#
# Declared explicitly even though 100 is also Azure's default: while it was
# absent, the setting was invisible to anyone reading this repo — you could grep
# the whole tree for "sampling" and conclude App Insights had none, when in fact
# a second knob was sitting there at its implicit default, able to throttle
# everything APIM sent.
variable "sampling_percentage" {
  type    = number
  default = 100
}

resource "azurerm_log_analytics_workspace" "law" {
  name                = "${var.name_prefix}-law"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  sku                 = "PerGB2018"
  retention_in_days   = var.retention_in_days
}

resource "azurerm_application_insights" "appi" {
  name                = "${var.name_prefix}-appi"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
  application_type    = "web"
  workspace_id        = azurerm_log_analytics_workspace.law.id
  sampling_percentage = var.sampling_percentage
}
