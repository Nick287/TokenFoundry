# Architecture

**English** | [中文](architecture.zh.md)

Token Foundry is an Azure-native LLM token hub: a **control plane** (this repo's
FastAPI app + React portal) that turns operator intent into **APIM** gateway
configuration, plus a cloud-automatic path (**方案 A**) that onboards GitHub
Copilot accounts as load-balanced backend hubs. The gateway (APIM) is the data
plane; the control plane never proxies LLM traffic.

![Token Foundry architecture](architecture.png)

> The PNG above is the at-a-glance view. The Mermaid below is the maintainable
> source of truth — update it, then regenerate the PNG if needed.

## System layers

```mermaid
flowchart TB
    subgraph client[Clients]
        SDK[Provider SDKs<br/>OpenAI / Anthropic / Google]
    end

    subgraph dataplane[Data plane — APIM gateway]
        API[Per-provider APIs<br/>llm-openai / llm-anthropic / llm-google]
        POOL[Backend pools<br/>session affinity + circuit breaker]
        POL[Policies<br/>token-limit · emit-metric · Cosmos write]
    end

    subgraph hubs[GitModel hubs — one per GitHub account]
        H1[hub gha_A<br/>Copilot subscription]
        H2[hub gha_B<br/>Copilot subscription]
    end

    subgraph ctrl[Control plane — FastAPI + React]
        PORTAL[Portal SPA]
        APISRV[API: tenants / keys / routes /<br/>github-accounts / deploy-config]
        PROV[ApimProvisioner]
        TRUN[terraform_runner<br/>方案 A trigger/poll]
    end

    subgraph stores[Stores]
        KV[(Key Vault<br/>all secrets)]
        PG[(PostgreSQL<br/>metadata, no secrets)]
        COS[(Cosmos DB<br/>usage records)]
    end

    SDK -->|virtual key| API --> POOL --> H1 & H2
    POL -.usage doc.-> COS
    PORTAL --> APISRV --> PROV -->|ARM REST| API & POOL
    APISRV --> TRUN -->|workflow_dispatch| GHA[GitHub Action<br/>deploy-hub.yml]
    GHA -->|SP + terraform| H1 & H2
    APISRV -->|refs only| PG
    APISRV -->|set/get| KV
    TRUN -->|read state blob| TFSTATE[(tfstate storage)]
```

**The one invariant:** the control plane configures the gateway (management
plane) and **never sits in the request path**. LLM traffic goes client → APIM →
hub, metered by APIM policy. The control plane's job is to make APIM objects
(APIs, backends, pools, subscriptions) match PostgreSQL intent.

## 方案 A — cloud-automatic hub onboarding

"Adding a model" becomes "adding a GitHub account". The hub Terraform runs inside
a **GitHub Action** authenticated by a Service Principal — the control plane only
triggers + polls it and reads outputs from remote state. This is the best
isolation of the options tried: the SP creds live in GitHub repo secrets; the
control plane holds only a deploy PAT (can trigger a predefined workflow) + blob
read on the state.

```mermaid
sequenceDiagram
    participant U as Operator (Portal)
    participant CP as Control plane
    participant KV as Key Vault
    participant GH as GitHub Action (SP)
    participant TF as tfstate blob
    participant AZ as Azure (new hub RG)
    participant APIM as APIM

    U->>CP: device-flow login (Copilot account)
    CP->>KV: write gh-<id>-jobinput (oauth+admin+hubkey)
    CP->>GH: workflow_dispatch(deploy-hub.yml, correlation_id)
    GH->>KV: read jobinput (SP → Secrets User)
    GH->>AZ: terraform apply (SP auth) → hub Container App
    GH->>TF: write hubs/<id>.tfstate
    CP->>GH: poll run → success
    CP->>TF: read outputs (app_url, resource_group)
    CP->>APIM: add hub to 3 pools (session affinity)
    CP->>APIM: register chat models as pooled routes
    CP-->>U: account READY
```

### Prerequisite: deploy configuration (one-time)

Before 方案 A can run, the GitHub wiring must be in place — done in the Portal
(not a shell script):

```mermaid
flowchart LR
    SP[create-deployer-sp.sh] -->|deployer-sp-* creds| KV[(Key Vault)]
    OP[Operator pastes<br/>2 PATs in Portal] -->|store| KV
    OP -->|save triggers| PUSH[deploy_config._push_sp_to_github]
    KV -->|read SP creds + bootstrap PAT| PUSH
    PUSH -->|pynacl sealed-box REST| REPO[GitHub repo<br/>Actions secrets ARM_*<br/>+ variables HUB_* / TFSTATE_*]
    PUSH -->|set flag| FLAG[github-repo-configured=true]
    FLAG -.gates.-> ADD[Add GitHub account unlocks]
```

The bootstrap PAT (repo Administration/Secrets write) is used once to push the
SP creds; the deploy PAT (Actions RW) is what the control plane uses at runtime
to trigger + poll. Both stored in Key Vault so a later SP rotation can re-push.

## Business logic — the entity model

```mermaid
flowchart TB
    T[Tenant<br/>billing + isolation boundary<br/>RESELL / BYO / INTERNAL] --> P[Project<br/>cost grouping]
    P --> VK[Virtual Key<br/>= APIM subscription<br/>value in KV, ref in PG]
    T --> PROD[APIM Product<br/>package tier]
    MR[Model Route<br/>alias → provider + pool/backend<br/>owner_scope, auth_mode, pricing] --> API[Provider API in APIM]
    GA[GitHub Account<br/>Copilot sub → one hub] --> POOL[Backend pool member]
    POOL --> API
    VK -->|calls| API
```

- **Tenant** — the billing + isolation boundary. `RESELL` pools platform models
  and resells with markup; `BYO` isolates a customer's own key in Key Vault;
  `INTERNAL` is chargeback-only. Bound to an APIM product so keys can issue.
- **Project** — groups virtual keys under a tenant for cost tracking.
- **Virtual Key** — an APIM subscription key. The value is shown **once** and
  stored in Key Vault; PostgreSQL keeps only a reference.
- **Model Route** — a client-facing alias (`gpt-4o`) → `provider` + APIM
  pool/backend (`apim_backend_or_pool_id`) + `auth_mode` (MI / KV_SECRET) +
  pricing. Platform-pooled routes (`owner_scope=PLATFORM`, `tenant_id=NULL`) fan
  out across every GitHub-account hub; BYO routes bind one tenant's backend.
- **GitHub Account** — one Copilot subscription → one deployed hub → one member
  in each provider pool. Adding accounts adds pool members (idempotent), not
  duplicate routes.

## Where each secret lives

See [SECURITY.md](SECURITY.md) for the full table. In one line: **every real
secret is in Key Vault; PostgreSQL holds only references; Cosmos holds only the
virtual-key id, never its value.**
