# Pricing & Sizing

**English** | [中文](PRICING.zh.md)

What Token Foundry costs to run, by tier — from a "which SKU do I pick" angle.

> **Where these numbers come from.** Every unit price is pulled from the **Azure
> Retail Prices API** (`https://prices.azure.com/api/retail/prices`,
> **Southeast Asia**, USD, pay-as-you-go). Every quantity is read with `az`
> against the **running dev-17 environment** (SKUs, replica counts, vCPU/memory,
> storage sizes). The only estimate is Log Analytics' monthly ingested GB, given
> as a range.
>
> **dev-17 was reclaimed on 2026-08-09 and dev-18 on 2026-08-15; the current
> environment is dev-19.** The figures were not re-measured and do not need to
> be: all three come from the **same terraform**, and dev-19's apply was 45 added
> / 0 destroyed with a resource set identical to dev-18's. Same shapes, same unit
> prices, same money. dev-17 is still named here rather than swapped for dev-19
> because that is where the quantities were actually read.
>
> Two things have since REDUCED cost, not yet re-measured:
>
> * `ApiManagementGatewayLogs` collection was turned off (2026-08-15), roughly
>   −1 KB of Log Analytics ingestion per call.
> * The response-body read was removed from `_USAGE_TRACE`, shrinking `traces`
>   rows.
>
> ⚠️ **This page covers Azure infrastructure only, not GitHub Copilot usage.**
> The scale assumptions (account counts, concurrency tiers) come from partner
> test accounts and are for evaluation only; per-account capacity is a black box
> the upstream never discloses, and repeat runs on the same accounts are bimodal.
> **Customer capacity and cost must be re-measured on the customer's own
> accounts** — see [CAPACITY.zh.md §0](CAPACITY.zh.md).
>
> The previous version of this page was assembled by hand and priced a
> **different environment**: a Developer-tier APIM in Central US, with no Event
> Hubs, no audit blob store and no usage-capture store at all. The tables below
> are recomputed against the actual 2026-08 deployment.
>
> For another region or agreement, confirm on the
> [Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/).

---

## What we actually pay today (read from dev-17; dev-19 is identical in shape)

| Component | Configuration | **USD / month** |
|---|---|---|
| **APIM Standard v2** | 1 unit × 730h × $0.9589 | **700.00** |
| **Container App — hubs ×3** | 1 vCPU / 2 GiB each, `minReplicas=1` | **94.61** |
| **Event Hubs Capture** | 730h × $0.100 — **flat, volume-independent** | **73.00** |
| Log Analytics + App Insights | 2–15 GB × $2.99 | 5.98 – 44.85 |
| Event Hubs throughput unit | 1 TU × 730h × $0.030 | 21.90 |
| PostgreSQL B1ms | Burstable, 730h × $0.026 | 18.98 |
| Container App — control plane | 0.5 vCPU / 1 GiB, `minReplicas=1` | 15.77 |
| Container Registry Basic | 30.42d × $0.1666 | 5.07 |
| PostgreSQL storage | 32 GB × $0.138 | 4.42 |
| Cosmos DB serverless | RU + storage, at our volume | 0.50 – 3.00 |
| Storage accounts ×3 | audit + usage capture + tfstate, LRS | 0.50 – 2.00 |
| Key Vault | per-operation | 0.20 – 1.00 |
| Event Hubs ingress events | ~0.06M × $0.028/1M | 0.00 |
| APIM calls | ≤50M/mo included (we do ~0.06M) | 0.00 |
| **Total** | | **≈ $941 – $985 / month** |

**APIM is 71–74% of the bill.**

### Three counter-intuitive findings

**1. Event Hubs Capture alone costs $73/month — more than PostgreSQL + ACR +
Cosmos + all storage combined.** It bills per **throughput-unit-hour** ($0.100/h),
**independent of event volume**. A full day of load testing produced about 1,800
events. That money doesn't buy throughput; it buys having the feature switched on.

**2. Each additional GitHub account is a flat +$31.54/month, not "$5–40 depending
on traffic".** Every hub is 1 vCPU / 2 GiB with `minReplicas=1`, so it **never
scales to zero**. Container Apps' idle rate ($0.000004/vCPU-second) really is
1/8.5 of the active rate ($0.000034), but idle ≠ free. The same hub at 100%
active is $110.38/month, not $31.54.

**3. We are paying for a gateway rated at thousands of req/s in front of an
upstream that caps us at ~17 req/s.** See [CAPACITY.zh.md](CAPACITY.zh.md)
(Chinese only): sustainable
concurrency is 48, measured throughput ~17 RPS, and the ceiling comes from
**per-GitHub-account Copilot quota**, not from APIM. The gateway is heavily
over-provisioned.

---

## Cost levers (by amount, all at real unit prices)

| Lever | Saves / month | Cost of doing it |
|---|---|---|
| **hub `minReplicas` 1 → 0** (3 hubs) | −$94.61 | Cold-start latency. ~3.5s of the known fixed overhead is model-independent; a cold start adds to that. |
| **Turn off Event Hubs Capture** | −$73.00 | Needs a code change: the importer switches from polling blobs to consuming the Event Hub directly. Today the path is Capture → Blob → importer → Cosmos. |
| Log Analytics daily cap / sampling | up to −$40 | Currently `dailyQuotaGb = -1` (uncapped). This line grows fast with volume — see "Cost as call volume grows". |

**All three: $941 → about $773/month.**

### The $550 that is deliberately NOT on that list: APIM tier

Standard v2 → Basic v2 saves **$550/month**, and we **choose not to**. The reason
is recorded here so it doesn't get re-litigated every time someone reads the bill:

**To keep the door open for VNet integration.** VNet integration starts at
**Standard v2** (Basic v2 doesn't have it). Once the gateway needs to sit inside a
virtual network, migrating back up from Basic v2 costs more — in effort and
risk — than the monthly difference.

Two facts worth pinning down, because **Basic v2 is technically sufficient** —
which is what makes this a trade-off rather than a constraint:

| Claim | Reality |
|---|---|
| "Only Standard v2 supports Anthropic" | ❌ The docs say **v2 tiers** (plural). `llm-token-limit`'s **APPLIES TO** line lists `Basic v2` verbatim; `llm-emit-token-metric` is **All API Management tiers**. Both note the Anthropic Messages API as "currently supported in API Management **v2 tiers**". |
| Classic tiers (Developer / Basic / Standard / Premium) | ❌ Do **not** support native Anthropic metering — v2 genuinely is the hard requirement. |
| Basic v2 included calls | 10M/month, then $0.030 per 10K (**$3 per extra million**). 20M/month = $150 + $30 = $180. |

> The exact wording is "Anthropic Messages API (currently supported in API
> Management v2 tiers)". `currently` is Microsoft's own hedge, and there is no
> per-tier support matrix for the Anthropic schema — so if we ever do move down
> to Basic v2, test it rather than trusting that sentence alone.

---

## Cost as call volume grows

Extrapolated from coefficients measured today (**1.74 KB per usage document**,
**3.85 KB of billed telemetry per call**, **~17 RPS ceiling on 3 accounts**):

| | Today (dev) | **10M calls/mo** | **20M calls/mo** |
|---|---|---|---|
| Average RPS | ~0 | 3.86 | 7.72 |
| GitHub accounts needed¹ | 3 | **3** | **5** |
| Container App — hubs | 94.61 (idle) | **253 – 331** | **370 – 552** |
| APIM Standard v2 | 700.00 | 700.00 | 700.00 |
| Log Analytics (no sampling) | 5.98 – 44.85 | **109.6** | **219.2** |
| Event Hubs (TU + Capture) | 94.90 | 94.90 | 94.90 |
| Cosmos² | 0.50 – 3.00 | **30.23** | **37.10** |
| Rest (PG / ACR / KV / Blob / control plane) | 45.00 | 55.00 | 55.00 |
| **Total** | **$941 – 985** | **$1,244 – 1,322** | **$1,476 – 1,658** |
| Per 1,000 calls | — | $0.124 – 0.132 | $0.074 – 0.083 |

¹ Assumes a **3× peak-to-average ratio**: peak RPS ÷ ~5.7 RPS per account
(measured). That ratio is an assumption, not a measurement, and it drives both
the account count and the hub row — **it is the most sensitive input in the
table**.

² **`cosmos_throughput_rus` defaults to 0 (serverless).** It briefly defaulted
to 400 (provisioned); dev-18 was built to test that and disproved it. The
surface arithmetic is persuasive: provisioned works out to $0.0222 per million
RU against serverless's $0.285 — **12.8× cheaper** — so it appears to win above
roughly **8% utilisation**, about 8M calls/month at 400 RU/s.

> ⚠️ **The break-even is self-contradictory: the volume at which provisioned-400
> becomes cheaper is already the volume at which provisioned-400 does not work.**
> 8M calls/month averages ~33 RU/s. The dev-18 campaign averaged exactly that —
> and Cosmos throttled it **2,904 times**, sitting at 100% of the 400 RU/s
> ceiling for 45 minutes straight.
>
> The cause is **shape, not size**. Peak demand over a whole minute was 255 RU/s,
> comfortably under 400 — but Cosmos enforces per second, and this workload is
> spiky by construction: the importer flushes a whole Capture blob at once every
> 300s, and one cross-partition aggregate costs up to 5,000 RU, i.e. **12.5
> seconds of a 400 RU/s budget in a single query**. Clearing that needs 1000+
> RU/s, which pushes break-even past ~20M calls/month. Serverless has no ceiling
> to hit, which is why four earlier campaigns never surfaced any of this.
>
> **Nothing was lost either way** — the importer only advances its watermark
> after a whole blob is in, and the SDK retries 429 — so the cost of getting
> this wrong is import latency, not billing gaps. See
> [CAPACITY.zh.md](CAPACITY.zh.md) §7.6.

> ⚠️ **This switch only works at environment-creation time, and terraform's plan
> will not tell you that.** `EnableServerless` is an account-level capability
> fixed at creation, and azurerm treats `capabilities` as computed — so removing
> it from the config produces **no diff at all**. The plan shows a single benign
> "container updated in-place, + throughput = 400" and the apply then fails on
> Azure's side. (Verified on dev-17: the account appears in the plan only under
> "Refreshing state", with zero planned changes.) The failure is safe — nothing
> is destroyed — but the switch cannot be made this way.
>
> dev-17 was therefore pinned to `cosmos_throughput_rus = 0` in
> `terraform.tfvars`. **0 is now the default**, so a new environment writes
> nothing at all — dev-19 has no such line and came up serverless (verified:
> `capabilities: [{"name":"EnableServerless"}]`). The reverse is what now needs
> pinning: an environment already CREATED provisioned must state its own value,
> or the next apply tries to make it serverless and fails. dev-18 pinned 400.

**The cost structure inverts as volume grows.** APIM is 71–74% of today's bill;
by 20M calls/month **hub Container Apps + Log Analytics together exceed it**. At
that point the priority stops being "pick a tier" and becomes "**turn on sampling,
control the duty cycle**".

> **Container Apps duty cycle is where the range comes from.** A hub bills at the
> **active** rate ($0.000034/vCPU-second) whenever ≥1 request is in flight, and at
> the **idle** rate ($0.000004, 8.5× less) otherwise. At 20M calls/month each hub
> averages 4.6 requests in flight — even a trough at 1/3 of average still has
> ~1.5, so it stays **active**. 100% active is therefore the upper bound; only
> genuinely dead nights land at the bottom of the range.

---

## APIM — the tier table

### Classic tiers (older Central US list prices, not re-pulled — we don't deploy classic)

| Tier | Price / month | Throughput / unit¹ | SLA | Built-in cache | Scale units | Multi-region | Good for |
|---|---|---|---|---|---|---|---|
| **Consumption** | $0 for first 1M ops, then $0.042 / 10K ops | auto | 99.95% | external only | auto | ❌ | spiky / serverless, low steady volume |
| **Developer** | **$48.04** | **500 req/s** | ❌ **none** | 10 MB | 1 (fixed) | ❌ | evaluation, dev, demos |
| **Basic** | **$147.17** | **1,000 req/s** | 99.95% | 50 MB | 2 | ❌ | entry-level production |
| **Standard** | **$686.72** | **2,500 req/s** | 99.95% | 1 GB | 4 | ❌ | medium-volume production |
| **Premium** | **$2,795.17** (per unit²) | **4,000 req/s** | 99.99%³ | 5 GB | 12 / region | ✔️ | high-volume / enterprise / HA |

### v2 tiers

Real Southeast Asia unit prices (Retail Prices API, 2026-08):

| Tier | $/hour/unit | **$/month** | Included requests | SLA | Cache | VNet |
|---|---|---|---|---|---|---|
| **Basic v2** | $0.20548 | **$150.00** | 10M/mo, then $0.030 / 10K | 99.95% | 250 MB | ❌ |
| **Standard v2** ← *ours* | $0.95890 | **$700.00** | 50M/mo, then $0.025 / 10K | 99.95% | 1 GB | VNet integration |
| **Premium v2** | $3.83562 | **$2,801.00** | unlimited | 99.99% | 5 GB | VNet integration + injection |

> Premium v2 incremental units are $1.91781/hour ($1,400/month), i.e. 50% of the
> first unit. Standard v2 secondary units are $0.68493/hour ($500/month).

¹ **Throughput is an official guideline, NOT a hard limit or SLA.** Azure's own
note: the numbers come from a test with 1,000 concurrent HTTPS connections,
minimal payloads, **no policies**, and a low-latency backend. Our policies
(token-limit, emit-metric, the per-key `<choose>` quota block) add per-request
work, so **real throughput is lower** — load-test before you size. In our case it
is moot: the ceiling is upstream, at ~17 RPS.
² Premium: incremental units of the same instance are charged at **50% of the
first unit**.
³ 99.99% requires ≥1 unit deployed across two or more availability zones or
regions.

---

## The rest of the environment — real unit prices and quantities

What Terraform actually provisions (see `terraform/modules/`). **Unit prices from
the Retail Prices API, quantities read from dev-17** — not estimates, except the
two rows given as ranges.

| Resource | SKU / configuration deployed | Unit price (Southeast Asia) | **$/month** |
|---|---|---|---|
| **Event Hubs namespace** | Standard, 1 TU, auto-inflate off | $0.030 / TU-hour | **21.90** |
| **Event Hubs Capture** | enabled, 300s / 100MB trigger | $0.100 / TU-hour | **73.00** |
| **Container App (control plane)** | 0.5 vCPU / 1 GiB, `minReplicas=1` | idle $0.000004/vCPU-s<br>memory $0.000004/GiB-s | **15.77** |
| **Container App (per hub)** | 1 vCPU / 2 GiB, `minReplicas=maxReplicas=1` | same | **31.54 each** |
| **PostgreSQL** | `Standard_B1ms` Burstable, 32 GB, 7-day backup | $0.026/hour + $0.138/GB-month | **23.40** |
| **Cosmos DB** | Serverless (new envs default to 400 RU/s provisioned) | $0.285 / 1M RU + $0.138/GB-month | 0.50 – 3.00 |
| **Log Analytics + App Insights** | PerGB2018, 30-day retention, **no daily cap** | $2.99 / GB ingested | 5.98 – 44.85 |
| **Container Registry** | Basic | $0.1666/day + $0.10/GB-month | **5.07** |
| **Storage accounts ×3** | Standard LRS (audit / usage capture / tfstate) | $0.020/GB-month + $0.050/10K writes | 0.50 – 2.00 |
| **Key Vault** | Standard | per-operation | 0.20 – 1.00 |

> **The Container Apps idle rate is not zero.** Active $0.000034/vCPU-second vs
> idle $0.000004 — 8.5× apart, but anything with `minReplicas ≥ 1` bills
> continuously. A hub at 100% active is **$110.38/month**; fully idle it is
> **$31.54/month**, and our dev load is close to the latter.
>
> **Each onboarded GitHub account is a flat +$31.54/month**, independent of
> traffic. Every hub is a *separate* Container App in its own resource group,
> with its own managed environment and managed identity.

---

## Whole-environment monthly estimate

At the real unit prices above, with hubs for **3 GitHub accounts**:

| Size | APIM | Everything else | **Total** |
|---|---|---|---|
| **Min** (Basic v2) | $150 | $241 – $285 | **≈ $391 – $435 / month** |
| **Medium** (Standard v2) ← *ours* | $700 | $241 – $285 | **≈ $941 – $985 / month** |
| **Max** (Premium v2 ×1) | $2,801 | $241 – $285 | **≈ $3,042 – $3,086 / month** |

The three biggest items inside "everything else" are **hub Container Apps
($94.61)**, **Event Hubs incl. Capture ($94.90)**, and **Log Analytics
($5.98–44.85, the only genuinely swingy one)**.

**Add $31.54 per onboarded GitHub account** — flat, not traffic-dependent.

---

## When to upgrade (from a user's angle)

**We're on Standard v2.** The measured data inverts the usual question — see
[CAPACITY.zh.md](CAPACITY.zh.md) (Chinese only):

> Sustainable concurrency **48**, measured ~**17 RPS**, ceiling set by
> **per-GitHub-account Copilot quota**, not APIM. Reproduced identically on
> dev-15, dev-16 and dev-17.

So the current APIM tier isn't a bottleneck, it's **over-provisioned** — and that
is **deliberate** (see "keeping the door open for VNet integration" above). To
raise throughput you add **GitHub accounts** (+$31.54/month idle, +$110.38 fully
loaded), **not APIM tier**.

APIM only needs to go **further up**, to Premium v2, when you need:

- **VNet injection** (Premium v2 only). Standard v2 has VNet *integration* only.
- **99.99% SLA** (Premium v2 only). Basic v2 and Standard v2 are both 99.95%.
- **More than 4 units, or multi-region.**

**Going down to Basic v2 is technically viable** (saves $550/month) and we choose
not to — reason above. If that's ever revisited, confirm two things first:
whether monthly calls will exceed the 10M included ($0.030/10K beyond, i.e. $3
per extra million), and whether Standard v2's VNet integration is by then in use.

**Right-sizing rule of thumb:**

- **Pure evaluation / demo, certain never to need VNet** → **Basic v2** ($150).
  Satisfies native Anthropic metering, 10M calls/month included, 99.95% SLA.
- **Customer-facing, one region, or wanting the VNet door open** → **Standard v2**
  ($700) ← *we are here*. 50M calls/month, 1 GB cache, VNet integration, up to
  4 units.
- **High volume, geo-distributed, HA** → **Premium v2** ($2,801/unit, incremental
  units at half price) — the only tier with 99.99% SLA and VNet injection.

> **Anthropic caveat (from our gateway):** APIM's *native* Anthropic Messages API
> support is **v2-tier only** (official wording: "currently supported in API
> Management **v2 tiers**"). We also treat Claude as a plain HTTP backend, so
> classic tiers can still *forward* it — see
> [docs/APIM-LLM-Gateway.md §4.6](APIM-LLM-Gateway.md). To have APIM **natively
> meter** Anthropic tokens you must be on a v2 tier; **both Basic v2 and Standard
> v2 qualify**.

---

## Cost levers, ranked by measured amount

> The APIM $550 is not on this list — see "deliberately not" above. What follows
> is what is **still undone with no reason not to do it**.

1. **Set hub `minReplicas` to 0** — saves **$94.61/month** across 3 hubs. Costs
   cold starts; today it is `minReplicas=maxReplicas=1`, never scaling to zero.
2. **Event Hubs Capture saves $73/month**, but needs a code change: have the
   importer consume the Event Hub directly instead of polling the blobs Capture
   writes. Note Capture bills a **flat TU-hour rate, independent of event
   volume** — a whole day of load testing produced ~1,800 events, so that money
   buys "switched on", nothing else.
3. **Log Analytics sampling / daily cap** — today `apim_sampling_percentage = 100`
   (every call logged) and `dailyQuotaGb = -1` (uncapped). At dev volume that is
   at most $44.85/month, but **it is the fastest-growing line as volume rises**.
   Measured billed telemetry is **3.85 KB per call**:

   | Table | B/call | Affected by 10% sampling? |
   |---|---|---|
   | `dependencies` | 1,286 | ✔️ |
   | `requests` | 1,218 | ✔️ |
   | `traces` (our `_USAGE_TRACE` policy) | 974 | ✔️ |
   | `customMetrics` (`llm-emit-token-metric`) | 346 | ❌ **not affected** |
   | `exceptions` | 116 | ✔️ |

   Per the docs, Request / Dependency / Exception / Trace are all emitted
   **per request** under the same diagnostic, so sampling applies to all four;
   custom metrics go through the separate `"metrics": true` switch and are
   **pre-aggregated time series** (1,811 requests produced only 774 rows). So 10%
   sampling is **3,940 B → 705 B, a 82% cut, not 90%** — those 346 B are a floor.

   10M calls/month $110 → $20; 20M calls/month $219 → $39.

   > The table above was measured in early 2026-08 and two entries have since
   > moved — correct them at the next re-measurement:
   >
   > * The 974 B for `traces` is too high. `_USAGE_TRACE` no longer reads the
   >   response body for a `usage` field (it flattened streaming — CAPACITY §7.7);
   >   five short attributes remain. **Not re-measured.**
   > * The table **omits `ApiManagementGatewayLogs`**, which was switched on at
   >   the time and measured ~**1 KB/call** on dev-19 — so the real total was
   >   closer to 4.9 KB than 3.85 KB. Collection stopped on 2026-08-15 (it
   >   duplicated `AppRequests` row for row and nothing read it), so this line is
   >   now zero.

   > ⚠️ **Sampling breaks the exact reconciliation.** The closure check counts
   > App Insights `requests`. Logging only 10% means extrapolating with
   > `sum(itemCount)`, degrading it from "equal to the record" into "a
   > statistical estimate". Exactness is what let 36 streaming refusals and a
   > 540-token gap each be traced to a cause. customMetrics and Cosmos are
   > unaffected; what's lost is **gateway-side request-count precision**.
   >
   > Two further traps: `always_log_errors = true` keeps every error while
   > sampling the successes, so **the failure rate is inflated** (1.3% renders as
   > ~7% at 10%); and breaker-shed 503s exist **only** in `requests` — Cosmos has
   > no document for them.
   >
   > A workable compromise: run at 10% normally and turn it back to 100% for a
   > reconciliation campaign (the APIM diagnostic property takes effect in
   > seconds, no rebuild). **Before lowering it, change the four KQL queries in
   > `app/services/usage_ingest.py` from `count()` to `sum(itemCount)`.**

4. **Cosmos defaults to serverless** (`cosmos_throughput_rus = 0`). It briefly
   defaulted to provisioned 400 until dev-18 disproved that — see footnote ²
   above. Existing environments can't be switched either way, and shouldn't be.

---

*Unit prices: Azure Retail Prices API (`prices.azure.com`), **Southeast Asia**,
USD, pay-as-you-go, pulled 2026-08. Quantities: `az` against dev-17 while it ran (since reclaimed; dev-19 is built from the same terraform and is identical in shape).
For another region or agreement, confirm on the
[Azure pricing calculator](https://azure.microsoft.com/en-us/pricing/calculator/).
The classic-tier table keeps its older Central US list prices and was not
re-pulled — we don't deploy classic tiers.*
