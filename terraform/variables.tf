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
variable "cosmos_throughput_rus" {
  description = <<-EOT
    Manual provisioned RU/s on the Cosmos `usage` container. 0 = serverless,
    which is the default and is the right answer for anything short of very
    high, very SMOOTH volume.

    The obvious arithmetic says otherwise and is wrong. Provisioned is $0.0222
    per million RU against serverless's $0.285 — 12.8x cheaper per RU — so
    provisioned looks like it wins above ~8% utilisation, about 8.2M calls/month
    at the 400 RU/s minimum. dev-18 was built that way to test it, and the
    figure collapses on contact:

      * 8.2M calls/month is ~32 RU/s on average.
      * The dev-18 campaign averaged 33 RU/s — the same load — and Cosmos
        throttled it 2,904 times, sitting at 100% of the 400 RU/s ceiling for
        45 minutes straight.

    So the volume at which provisioned-400 becomes cheaper is already the volume
    at which provisioned-400 does not work. Clearing it means 1000+ RU/s, which
    pushes break-even out past ~20M calls/month.

    The cause is SHAPE, not size. Peak demand over a whole minute was 255 RU/s,
    comfortably under 400 — but Cosmos enforces per second, and this workload is
    spiky by construction: the importer flushes a whole Capture blob at once
    every 300s, and one cross-partition aggregate costs up to 5,000 RU, i.e.
    12.5 seconds of a 400 RU/s budget in a SINGLE query. Serverless has no
    ceiling to hit, which is why four earlier campaigns never surfaced any of
    this.

    Nothing was lost either way — the importer only advances its watermark after
    a whole blob is in, and the SDK retries 429 — so the cost of getting this
    wrong is import latency, not billing gaps.

    Set a value here only for genuinely high, genuinely steady traffic, and size
    it for the spikes rather than the average.

    NOTE: this is a CREATION-TIME choice and terraform will not warn you
    otherwise. azurerm treats the account's `capabilities` as computed, so
    removing `EnableServerless` produces no diff at all — the plan shows a
    benign container update and the apply then fails on Azure's side (verified
    on dev-17). An environment already built the other way must pin its value
    here: dev-18 pins 400 because that is what it was created with.
  EOT
  type        = number
  default     = 0
}

variable "apim_sampling_percentage" {
  description = <<-EOT
    How much of the APIM gateway's telemetry reaches Application Insights, in
    percent. 100 = every request, which is the default and is what the
    reconciliation depends on.

    This is the SOURCE-side knob (how much APIM sends). It is written to the
    `applicationinsights` diagnostic and applies to the `requests`, `traces` and
    `dependencies` tables. It does NOT apply to customMetrics: per Azure docs,
    "metrics (including custom metrics) are never sampled". So token counts stay
    exact at any value here — the `metrics` flag is their on/off switch, not
    sampling. Billing is unaffected either way; that rides hub -> Event Hub ->
    Cosmos and never touches App Insights.

    LOWERING THIS BREAKS EXACT RECONCILIATION, and it is worth being explicit
    about why, because the numbers still look plausible afterwards:

      * The ledger check is `gateway 200+429+4xx == cosmos documents + hub lost`.
        The left side comes from `requests`, the right side does not. Sample the
        left and the equality is gone.
      * `always_log_errors = true` exempts errors, so ONLY the successes get
        sampled. At 10% a run of 1,739 successes and 13 failures reads as ~174
        and 13 — a 1.3% failure rate rendered as 7%.
      * Breaker-shed 503s exist ONLY in `requests`; Cosmos has no document for a
        request that never reached a hub. Today that was 252 of 2,004 calls.
      * `sum(itemCount)` recovers an ESTIMATE, not the exact count. The value of
        the current check is that it closes exactly — that is how 36 streaming
        refusals and a 540-token gap were each traced to a cause. A tolerance
        band would have hidden both.

    So before lowering this, change the four KQL queries in
    app/services/usage_ingest.py from `count()` to `sum(itemCount)`, and accept
    that the ledger becomes approximate. Reasonable range if ingestion cost ever
    justifies it: 5-20.

    0 IS ALLOWED and is a distinct posture, not just "very low". Combined with
    alwaysLog=allErrors — which is set on every API diagnostic and was verified
    working on dev-19 (2026-08-20: at 10%, successes stored 5 of 50 while all 17
    failures were kept) — zero means:

        successful requests : not logged at all
        failed requests     : every one still logged
        customMetrics       : untouched, metrics are never sampled
        Cosmos billing      : untouched, it never went through App Insights

    That is a cleaner trade than a low non-zero value: 10% keeps a random tenth
    of successes, which is neither cheap nor accurate. 0 says plainly "successes
    are not worth storing, failures are".

    What it costs: the gateway side of the reconciliation goes away entirely
    (the ledger's `gateway 200` term is unmeasurable), along with call counts,
    latency percentiles and the portal's traffic charts. Breaker-shed 503s
    survive, since they are errors.

    Reach for 0 when ingestion volume actually hurts — Azure warns that logging
    every event can cost 40-50% of throughput at high request rates — not to
    save the few dollars it amounts to at low volume.
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.apim_sampling_percentage >= 0 && var.apim_sampling_percentage <= 100
    error_message = "apim_sampling_percentage must be between 0 and 100."
  }
}

variable "app_insights_sampling_percentage" {
  description = <<-EOT
    Ingestion sampling on the Application Insights component itself, in percent.
    100 = keep everything, the default. LEAVE IT AT 100 and use
    apim_sampling_percentage as the knob — reason below.

    The two are in series: apim_sampling_percentage decides how much APIM sends,
    this decides how much App Insights keeps of what arrives. So turning the
    UPSTREAM one down definitely works (10 upstream + 100 here = 10 overall).

    Turning THIS one down may do nothing. Azure's docs say "if adaptive or
    fixed-rate sampling methods are enabled for a telemetry type, ingestion
    sampling is disabled for that specific type" — and the APIM diagnostic
    always sends samplingType = "fixed", even at 100. If APIM stamps its
    telemetry the way an SDK does, ingestion sampling is bypassed for
    requests/traces/dependencies entirely. Whether it does is UNVERIFIED here:
    every measurement on this project was taken with both knobs at 100, which
    cannot distinguish the two cases. Do not assume the percentages multiply.

    Same blast radius as the other knob if it applies at all: requests, traces
    and dependencies yes; customMetrics no (metrics are never sampled).

    It is declared mainly so it is FINDABLE. It was previously absent from the
    configuration entirely, running on Azure's implicit default, so anyone who
    went looking for it in the repo would conclude it did not exist.
  EOT
  type        = number
  default     = 100

  validation {
    condition     = var.app_insights_sampling_percentage > 0 && var.app_insights_sampling_percentage <= 100
    error_message = "app_insights_sampling_percentage must be in (0, 100]."
  }
}

variable "log_retention_days" {
  description = <<-EOT
    Log Analytics retention in days.

    NOT a cost lever. PerGB2018 includes the first 31 days free, so 7 and
    30 bill identically; ingestion is what costs money, and it is charged
    once per GB on the way in. Measured on dev-19: 28 MB over 7 days, of
    which 57% was the control plane's own console log — which is why the
    健康-probe access lines were filtered out rather than the retention cut.

    It IS a data-minimisation lever: the diagnostic has logClientIp on, so
    these tables carry caller IP addresses.

    LOWERING IT SAVES NOTHING. Azure's own wording: "lowering the retention
    period below 31 days does not reduce costs, as 31 days of analytics
    retention are included in the ingestion price." You pay per GB once, on the
    way IN; retention is only charged beyond 31 days.

    So 30 means "no retention charge, with a full month to debug from". The way
    to spend less on logs is to ingest less — which is why the health-probe
    access lines were filtered out (82% of the control plane's log volume) and
    the duplicate GatewayLogs collection was switched off, rather than the
    window being shortened.

    The provider also refuses under 30 anyway. Attempted 7 on 2026-08-20:

        Error: expected retention_in_days to be in the range (30 - 730), got 7

    rejected at plan time, before the request reached Azure. (Azure's API/CLI
    can go as low as 4 days, so this is a provider bound rather than a platform
    one — but going there would buy data minimisation only, never money.)
  EOT
  type        = number
  default     = 30

  validation {
    condition     = var.log_retention_days >= 30 && var.log_retention_days <= 730
    error_message = "PerGB2018 allows 30-730 days. Values under 30 are rejected by the provider before reaching Azure, and would not save anything: the first 31 days of retention are included in the per-GB ingestion charge."
  }
}

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
