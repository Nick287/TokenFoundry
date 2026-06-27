# Token Foundry

Azure-native LLM token hub / AI gateway. A hybrid control plane on top of Azure
API Management's GenAI gateway — multi-provider (Anthropic Claude / Google
Gemini / OpenAI), per-tenant virtual keys, token/cost metering, and a React
portal. Each provider gets its own APIM API with the provider's native
subscription-key header, so the provider's own SDK works against the gateway.

## Architecture

```
                  aca-app (single Container App)
                  ┌─────────────────────────────────────┐
                  │  React portal (admin + customer SPA) │
                  │  served by FastAPI StaticFiles       │
                  │            │ /api                     │
  Clients ─key─▶ APIM         ▼                           │
   (data plane)  │   FastAPI control plane               │
   Unified API   │   tenants / keys / routes / budgets / │
   token-limit   │   usage  +  tenant-scope auth         │
   LB+breaker    └───────────────┬─────────────────────┘
   emit-metric        │          ├─ PostgreSQL  (metadata)
        │             ▼          ├─ Cosmos NoSQL (usage)
   Claude / Gemini / OpenAI      ├─ Key Vault   (secrets)
   backends (per-provider API)   └─ Azure Monitor + Grafana
```

- **APIM = data plane** — limiting, routing, caching, metrics via GenAI policies.
- **FastAPI = control plane** — provisioning + accounting + enforcement only;
  also serves the built SPA (one image, one Container App, no nginx).
- **React = human layer** — operator console (admin) + customer portal (customer).

## Layout

```
app/            FastAPI control plane (models / services / api)
worker/         Event Hub consumer (Phase 2)
portal/         React + Vite frontend
infra/          Bicep IaC (main + modules + Grafana dashboards)
apim/policies/  APIM policy XML (data-plane core)
tests/          pytest (billing logic — pure, no Azure)
```

## Run it (inside the Dev Container)

Open the repo in the Dev Container (VS Code: "Reopen in Container"). It installs
Python + Node + azure-cli (with `az bicep`) and runs `pip install -e .[dev]` and
`npm install`.

### 1. Authenticate to Azure

```bash
az login
az account set --subscription <your-sub-id>
```

`DefaultAzureCredential` (backend) and `az deployment` (Bicep) both reuse this.

### 2. Validate everything (no cloud needed)

```bash
# Backend: lint, type-check, unit tests
ruff check app worker tests
mypy app
pytest -q

# Frontend: type-check + production build
cd portal && npm run typecheck && npm run build && cd ..

# Bicep: compile + preview against a resource group
az bicep build --file infra/main.bicep
az deployment group what-if -g <rg> -f infra/main.bicep \
  -p infra/main.bicepparam -p pgAdminPassword=<pwd>
```

### 3. Run the stack locally

```bash
# Backend (needs a local Postgres or TF_DATABASE_URL pointing at one)
cp .env.example .env          # fill TF_* values
uvicorn app.main:app --reload --port 8000

# Frontend (separate terminal)
cd portal
cp .env.example .env          # VITE_DEV_TOKEN=dev:admin: for local admin
npm run dev                   # http://localhost:5173, proxies /api -> :8000
```

Local auth uses a dev token (`dev:<role>:<tenant>`) that the backend accepts only
when `TF_ENVIRONMENT=local` — no Entra needed to exercise the flow end-to-end.

### 4. Deploy

```bash
az group create -n <rg> -l <region>
az deployment group create -g <rg> -f infra/main.bicep \
  -p infra/main.bicepparam -p pgAdminPassword=<pwd>
```

Then build/push the single image to ACR and update `appImage`:

```bash
az acr build -r <acr> -t tokenfoundry:latest .   # root Dockerfile builds portal + API
```

## Verification (maps to the plan's end-to-end checklist)

1. `az login` in the container.
2. `az deployment group create …` — APIM / PostgreSQL / Cosmos / Monitor /
   Grafana up; backend pool + circuit breaker created (preview API version).
3. Admin console → create tenant + issue key + add model alias → APIM gets the
   Product/Subscription/backend, key lands in Key Vault.
4. Call a provider API with the key (e.g. `POST {gateway}/llm-openai/v1/chat/completions`
   with the virtual key in the `api-key` header) → completion; over-TPM → 429.
5. Multi-provider: switch the `model` in the body and call the matching provider
   path — `claude-*` → `/llm-anthropic/v1/messages` (`x-api-key` header),
   `gpt-5.x` → `/llm-openai/v1/responses`, other OpenAI/Gemini →
   `/v1/chat/completions` → all route correctly.
6. App Insights shows token metrics by tenant/route; `GET /usage` agrees.
7. Grafana renders cross-tenant usage/cost/TPM.
8. Small budget → `budget_enforcer` suspends the subscription → 401 thereafter.
9. Azure Monitor alert → Action Group on budget threshold.
10. (Phase 2) Customer portal: customer sees only their tenant; cross-tenant
    access rejected by the tenant-scope middleware.
11. (Phase 2) BYO backend credential isolation; semantic cache stays per-tenant.

To smoke-test every registered model end-to-end through the gateway, run
`python scripts/smoke_test_models.py` — it auto-discovers the models from the
control plane, calls each through its provider path (routing `gpt-5.x` to the
Responses API), and prints a pass/fail table. Configure the gateway URL and a
virtual key via a local `.env` (gitignored); see the script's header for the
required variables.

## Implementation status

- **Phase 1 (this scaffold):** data model, control-plane API + tenant-scope
  auth, APIM provisioning service, multi-provider model routes, admin console,
  Bicep for all PaaS, token-limit + emit-token-metric policy, Grafana dashboard.
- **Phase 0 to do first:** validate the Unified Model API *management* contract
  for adding a model/alias against a live instance (see `apim_provisioner.attach_alias`).
- **Phase 2:** Event Hub billing worker, semantic cache, BYO isolation, customer
  portal, budget $-enforcement via the stream, chargeback.
