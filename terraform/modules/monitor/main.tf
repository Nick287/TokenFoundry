# Log Analytics workspace + Application Insights — Azure Monitor foundation.

variable "name_prefix" { type = string }
variable "location" { type = string }
variable "tags" { type = map(string) }
variable "resource_group_name" { type = string }

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
  retention_in_days   = 30
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
