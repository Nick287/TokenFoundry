# Pricing & Sizing

**English** | [中文](PRICING.zh.md)

What Token Foundry costs to run, by tier — from a "which SKU do I pick" angle.
The APIM prices and throughput below are the **official Azure list prices**
(Central US, USD, per month, as shown on the Azure pricing page). Everything else
is a close estimate — **always confirm against the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/)**
for your region, currency, and agreement.

> APIM is the cost driver and the throughput ceiling of the whole stack, so this
> doc leads with it. The other resources (Postgres, Cosmos, Key Vault, ACR,
> monitoring, storage) are comparatively cheap and mostly usage-based.

---

## TL;DR — three sizes

| | **Min (evaluation)** | **Medium (production)** | **Max (enterprise)** |
|---|---|---|---|
| **APIM tier** | Developer | Standard | Premium |
| **APIM / month** | **$48** | **$687** | **$2,795** (per unit) |
| **Throughput** | ~500 req/s | ~2,500 req/s | ~4,000 req/s (per unit, scale to 12/region) |
| **SLA** | ❌ none | 99.95% | 99.99% (multi-AZ/region) |
| **Scale units** | 1 (can't scale) | up to 4 | up to 12 per region, multi-region |
| **Whole env / month (est.)** | **≈ $80–110** | **≈ $800–1,000** | **≈ $3,000–3,500+** |
| **Who it's for** | dev / demo / this repo's dev-a0x | real customers, one region, needs an SLA | high volume, geo-distributed, HA |

> **We currently run `Developer_1`** (the Min column) — deliberately: it's for
> evaluation, has **no SLA**, and **cannot be scaled**. The first thing to change
> before serving real traffic is APIM tier (see "When to upgrade" below).

---

## APIM — the tier table (official list prices, Central US)

### Classic tiers

| Tier | Price / month | Throughput / unit¹ | SLA | Built-in cache | Scale units | Multi-region | Good for |
|---|---|---|---|---|---|---|---|
| **Consumption** | $0 for first 1M ops, then $0.042 / 10K ops | auto | 99.95% | external only | auto | ❌ | spiky / serverless, low steady volume |
| **Developer** ← *ours* | **$48.04** | **500 req/s** | ❌ **none** | 10 MB | 1 (fixed) | ❌ | evaluation, dev, demos |
| **Basic** | **$147.17** | **1,000 req/s** | 99.95% | 50 MB | 2 | ❌ | entry-level production |
| **Standard** | **$686.72** | **2,500 req/s** | 99.95% | 1 GB | 4 | ❌ | medium-volume production |
| **Premium** | **$2,795.17** (per unit²) | **4,000 req/s** | 99.99%³ | 5 GB | 12 / region | ✔️ | high-volume / enterprise / HA |

### v2 tiers (faster provisioning, VNet integration)

| Tier | Price / month | Included requests | SLA | Cache | VNet |
|---|---|---|---|---|---|
| **Basic v2** | $150.01 | 10M / mo, then $3 / 1M | 99.95% | 250 MB | ❌ |
| **Standard v2** | $700 | 50M / mo, then $2.50 / 1M | 99.95% | 1 GB | VNet integration |
| **Premium v2** | $2,801 | unlimited | 99.99% | 5 GB | VNet integration + injection |

¹ **Throughput is an official guideline, NOT a hard limit or SLA.** Azure's own
note: the numbers come from a test with 1,000 concurrent HTTPS connections,
minimal payloads, **no policies**, and a low-latency backend. Our policies
(token-limit, emit-metric, the Cosmos outbound write, the per-key `<choose>`
quota block) add per-request work, so **real throughput is lower** — load-test
before you size.
² Premium: incremental units of the same instance are charged at **50% of the
first unit**.
³ 99.99% requires ≥1 unit deployed across two or more availability zones or
regions.

---

## The rest of the environment (per month, estimates)

These are what Terraform actually provisions (see `terraform/modules/`), at the
SKUs we deploy. Prices are **rough estimates** — confirm on the calculator.

| Resource | SKU we deploy | Billing model | Est. / month | Notes |
|---|---|---|---|---|
| **PostgreSQL** | `B_Standard_B1ms` (Burstable, 1 vCore) | per-hour + storage | ≈ $15–20 | Cheapest burstable. Bump to General Purpose for production load. |
| **Cosmos DB** | Serverless | per-RU + storage | ≈ $5–25 | Pay per request. Scales with usage-record write volume + 90-day TTL. |
| **Key Vault** | Standard | per-operation | ≈ $1–5 | Fractions of a cent per secret op; negligible at our volume. |
| **Container Registry** | Basic | flat + storage | ≈ $5 | Holds `tokenfoundry:<tag>` + `gitmodel:<tag>`. |
| **Log Analytics + App Insights** | PerGB2018 | per-GB ingested | ≈ $5–50 | Scales with telemetry volume; the swingiest line item. Sampling caps it. |
| **Storage (tfstate)** | Standard LRS | per-GB + ops | < $1 | Tiny — a few KB of Terraform state blobs. |
| **Container App (control plane)** | Consumption | per vCPU-s + mem-s | ≈ $5–40 | Scales to near-zero when idle; one always-on replica costs more. |

> **Per-GitHub-account GitModel hub** is a *separate* Container App in its own
> resource group — one more Consumption-plan Container App per account (≈ $5–40
> each depending on traffic). Each account you onboard adds one.

---

## Whole-environment monthly estimate

| Size | APIM | Everything else | **Total (est.)** |
|---|---|---|---|
| **Min** (Developer) | $48 | ≈ $35–60 | **≈ $80–110 / month** |
| **Medium** (Standard) | $687 | ≈ $100–300 | **≈ $800–1,000 / month** |
| **Max** (Premium ×1) | $2,795 | ≈ $200–700 | **≈ $3,000–3,500 / month** |

Add **≈ $5–40 per onboarded GitHub account** (each is its own hub Container App).
"Everything else" swings mostly on **Log Analytics ingestion** and **Container
App** always-on vs scale-to-zero.

---

## When to upgrade (from a user's angle)

**You're on Developer (our default). Move off it when ANY of these is true:**

- **You need an SLA.** Developer has **none** — Azure won't credit you for
  downtime. Basic/Standard give 99.95%, Premium 99.99%. This alone rules
  Developer out for anything customer-facing.
- **You're approaching ~500 req/s** (Developer's guideline, and lower with our
  policies). Next stops: Basic (1,000), Standard (2,500), Premium (4,000/unit).
- **You need to scale.** Developer is **fixed at 1 unit** — it cannot scale out.
  Every other tier adds units; Premium adds units *and* regions.
- **You need multi-region / availability zones.** Only **Premium** (classic)
  distributes one instance across regions and supports AZs for the 99.99% SLA.

**Right-sizing rule of thumb:**

- **Evaluation / internal / this repo's dev environments** → **Developer** ($48).
  What we run now.
- **First real customers, one region, need an SLA** → **Standard** ($687) — the
  sweet spot: SLA + 2,500 req/s + autoscale to 4 units, no Premium premium.
- **High volume, geo-distributed, HA** → **Premium** ($2,795/unit) — the only
  classic tier with multi-region + AZ.
- **Spiky, low steady traffic** → consider **Consumption** (pay-per-call) instead
  of a dedicated tier — but note it has no built-in cache and different policy
  support.

> **Anthropic caveat (from our gateway):** APIM's *native* Anthropic Messages API
> support is **v2-tier only**. We sidestep that by treating Claude as a plain HTTP
> backend, so it works on classic Developer/Standard/Premium too — see
> [docs/APIM-LLM-Gateway.md §4.6](APIM-LLM-Gateway.md). If you ever want APIM to
> parse Anthropic requests natively, that pushes you to a **v2** tier.

---

## Cost levers (how to spend less)

1. **APIM tier is 80%+ of the bill.** Don't over-provision — start at the tier
   your SLA + throughput actually require, scale units up later (all non-Developer
   tiers autoscale).
2. **Log Analytics ingestion** is the second-biggest swing. APIM's diagnostic
   sampling is the knob; lower it to cut telemetry cost.
3. **Container Apps scale to zero** when idle — keep min-replicas at 0 for
   non-critical hubs to avoid always-on charges.
4. **Cosmos is serverless** — you only pay for the usage-record writes + 90-day
   TTL storage. No idle cost.
5. **Consumption APIM** bills per call — for a low-steady-volume gateway it can be
   cheaper than even Developer, with an SLA. Trade-off: no built-in cache, cold
   starts, and reduced policy support.

*Prices as shown on the Azure pricing page for Central US, USD. Estimates for
non-APIM resources are indicative — confirm on the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/).*
