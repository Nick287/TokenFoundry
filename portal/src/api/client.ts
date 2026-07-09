// Typed fetch client for the FastAPI control plane.
// Attaches the bearer token; throws on non-2xx so React Query surfaces errors.

const API_BASE = import.meta.env.VITE_API_BASE ?? "/api";

export interface TokenResponse {
  access_token: string;
  token_type: string;
  role: "admin" | "customer";
  tenant_id: string | null;
}

export interface Tenant {
  id: string;
  name: string;
  mode: "RESELL" | "BYO" | "INTERNAL";
  status: string;
  apim_product_ids: string[];
  created_at: string;
}

export interface Project {
  id: string;
  tenant_id: string;
  name: string;
  cost_center: string | null;
  created_at: string;
}

export interface User {
  id: string;
  username: string;
  role: "admin" | "customer";
  tenant_id: string | null;
  disabled: boolean;
  created_at: string;
}

export interface VirtualKey {
  id: string;
  project_id: string;
  status: string;
  allowed_route_ids: string[];
  tokens_per_minute: number | null;
  token_quota_tier: string | null;
  token_quota_period: string | null;
  created_at: string;
}

export interface VirtualKeySecret extends VirtualKey {
  key_value: string;
}

export interface ModelRoute {
  id: string;
  name: string;
  provider: "openai" | "anthropic" | "google" | "azure";
  owner_scope: "PLATFORM" | "TENANT";
  deployment_name: string | null;
  api_version: string | null;
  price_in_per_1k: number;
  price_out_per_1k: number;
  markup_pct: number;
  created_at: string;
}

export type GitHubDeployStatus =
  | "pending"
  | "deploying"
  | "ready"
  | "failed"
  | "deleting";

// A GitHub account whose Copilot subscription backs one deployed GitModel hub.
// Mirrors app/models/schemas.py:GitHubAccountOut.
export interface GitHubAccount {
  id: string;
  github_login: string | null;
  status: GitHubDeployStatus;
  error_detail: string | null;
  resource_group: string | null;
  container_app_fqdn: string | null;
  backend_ids: string[];
  created_at: string;
}

// Returned by POST /github-accounts/device/start — what the user needs to
// authorize the GitHub Copilot account in their browser.
export interface DeviceStart {
  account_id: string;
  user_code: string;
  verification_uri: string;
  interval: number;
  expires_in: number;
}

// Returned by POST /github-accounts/device/poll — current auth+deploy state.
export interface DevicePoll {
  account_id: string;
  status: GitHubDeployStatus;
  github_login: string | null;
  detail: string | null;
}

// Readiness of the 方案 A GitHub deploy wiring (drives the add-account gate).
// Never carries secret VALUES — only presence booleans. Mirrors
// app/models/schemas.py:DeployConfigStatus.
export interface DeployConfigStatus {
  bootstrap_pat_set: boolean;
  deploy_pat_set: boolean;
  sp_creds_present: boolean;
  pushed: boolean;
  ready: boolean;
  detail: string | null;
}

export interface UsageSummary {
  tenant_id: string;
  total_prompt_tok: number;
  total_completion_tok: number;
  total_cost_usd: number;
  total_billed_usd: number;
}

// One row in the Cosmos-sourced call log (per-call usage record).
export interface UsageRecordView {
  ts: string | null;
  subscription: string | null;
  project_id: string | null;
  project_name: string | null;
  route: string;
  api: string | null;
  prompt_tok: number;
  completion_tok: number;
  cached_tok: number;
}

// One server-side page of the Cosmos-sourced call log.
export interface UsageRecordPage {
  items: UsageRecordView[];
  total: number;
  page: number;
  page_size: number;
}

// App Insights-sourced call counts + latency (separate data source).
export interface UsageTelemetry {
  total_calls: number;
  by_api: Array<{
    name: string;
    calls: number;
    p50: number | null;
    p95: number | null;
    failures: number;
    gateway_p50: number | null;
    backend_p50: number | null;
  }>;
  by_hour: Array<{ ts: string; calls: number }>;
}

// Per-model (or per-endpoint / per-subscription) token breakdown from App
// Insights metering. Covers streaming + non-streaming. Each group carries the
// five token types + metered call count; `trend` is a zero-filled dual series
// (tokens + calls) on the same buckets.
export interface TokenGroup {
  model?: string;
  api?: string;
  subscription?: string;
  backend?: string;
  total: number;
  prompt: number;
  cached: number;
  completion: number;
  reasoning: number;
  cache_creation: number;
  // Emitted for multimodal / speculative-decoding; 0 for plain-text calls.
  accepted_prediction?: number;
  rejected_prediction?: number;
  prompt_audio?: number;
  completion_audio?: number;
  calls: number;
}
export interface UsageBreakdown {
  by: "model" | "api" | "subscription" | "backend";
  hours: number;
  groups: TokenGroup[];
  trend: Array<{ ts: string; tokens: number; calls: number }>;
  totals: {
    total: number;
    prompt: number;
    cached: number;
    completion: number;
    reasoning: number;
    cache_creation: number;
    accepted_prediction?: number;
    rejected_prediction?: number;
    prompt_audio?: number;
    completion_audio?: number;
    calls: number;
  };
}

async function request<T>(
  path: string,
  token: string,
  init?: RequestInit,
): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json() as Promise<T>;
}

async function requestNoContent(
  path: string,
  token: string,
  init?: RequestInit,
): Promise<void> {
  const res = await fetch(`${API_BASE}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${token}`,
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`${res.status}: ${detail}`);
  }
}

export const api = {
  login: async (username: string, password: string): Promise<TokenResponse> => {
    const res = await fetch(`${API_BASE}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });
    if (!res.ok) {
      const detail = await res.text();
      throw new Error(`${res.status}: ${detail}`);
    }
    return res.json() as Promise<TokenResponse>;
  },
  listTenants: (token: string) => request<Tenant[]>("/tenants", token),
  createTenant: (token: string, body: { name: string; mode: string }) =>
    request<Tenant>("/tenants", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  ensureTenantProduct: (token: string, tenantId: string) =>
    request<Tenant>(`/tenants/${tenantId}/ensure-product`, token, {
      method: "POST",
    }),
  updateTenant: (token: string, id: string, body: { name?: string; mode?: string }) =>
    request<Tenant>(`/tenants/${id}`, token, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteTenant: (token: string, id: string) =>
    requestNoContent(`/tenants/${id}`, token, { method: "DELETE" }),
  listProjects: (token: string, tenantId?: string) =>
    request<Project[]>(
      tenantId ? `/projects?tenant_id=${encodeURIComponent(tenantId)}` : "/projects",
      token,
    ),
  createProject: (
    token: string,
    body: { tenant_id: string; name: string; cost_center?: string | null },
  ) =>
    request<Project>("/projects", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateProject: (
    token: string,
    id: string,
    body: { name?: string; cost_center?: string | null },
  ) =>
    request<Project>(`/projects/${id}`, token, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteProject: (token: string, id: string) =>
    requestNoContent(`/projects/${id}`, token, { method: "DELETE" }),
  listRoutes: (token: string) => request<ModelRoute[]>("/routes", token),
  createRoute: (token: string, body: Record<string, unknown>) =>
    request<ModelRoute>("/routes", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateRoute: (token: string, id: string, body: Record<string, unknown>) =>
    request<ModelRoute>(`/routes/${id}`, token, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  deleteRoute: (token: string, id: string) =>
    requestNoContent(`/routes/${id}`, token, { method: "DELETE" }),
  createKey: (token: string, body: Record<string, unknown>) =>
    request<VirtualKeySecret>("/keys", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listKeys: (token: string, projectId?: string) =>
    request<VirtualKey[]>(
      projectId ? `/keys?project_id=${encodeURIComponent(projectId)}` : "/keys",
      token,
    ),
  deleteKey: (token: string, id: string) =>
    requestNoContent(`/keys/${id}`, token, { method: "DELETE" }),
  myUsage: (token: string) => request<UsageSummary>("/usage", token),
  tenantUsage: (token: string, tenantId: string) =>
    request<UsageSummary>(`/admin/usage/${tenantId}`, token),
  tenantUsageRecords: (
    token: string,
    tenantId: string,
    page = 1,
    pageSize = 25,
  ) =>
    request<UsageRecordPage>(
      `/admin/usage/${tenantId}/records?page=${page}&page_size=${pageSize}`,
      token,
    ),
  usageTelemetry: (token: string) =>
    request<UsageTelemetry>("/admin/usage-telemetry", token),
  usageBreakdown: (
    token: string,
    tenantId: string,
    hours = 24,
    by: "model" | "api" | "subscription" | "backend" = "model",
  ) =>
    request<UsageBreakdown>(
      `/admin/usage/${tenantId}/breakdown?hours=${hours}&by=${by}`,
      token,
    ),
  platformUsageBreakdown: (
    token: string,
    hours = 24,
    by: "model" | "api" | "subscription" | "backend" = "model",
  ) =>
    request<UsageBreakdown>(
      `/admin/usage-breakdown?hours=${hours}&by=${by}`,
      token,
    ),
  listUsers: (token: string) => request<User[]>("/users", token),
  createUser: (
    token: string,
    body: { username: string; password: string; role: string; tenant_id?: string | null },
  ) =>
    request<User>("/users", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  updateUser: (
    token: string,
    id: string,
    body: { role?: string; tenant_id?: string | null; disabled?: boolean },
  ) =>
    request<User>(`/users/${id}`, token, {
      method: "PATCH",
      body: JSON.stringify(body),
    }),
  resetPassword: (token: string, id: string, newPassword: string) =>
    request<User>(`/users/${id}/reset-password`, token, {
      method: "POST",
      body: JSON.stringify({ new_password: newPassword }),
    }),
  deleteUser: (token: string, id: string) =>
    requestNoContent(`/users/${id}`, token, { method: "DELETE" }),
  changeMyPassword: (token: string, oldPassword: string, newPassword: string) =>
    requestNoContent("/me/password", token, {
      method: "POST",
      body: JSON.stringify({ old_password: oldPassword, new_password: newPassword }),
    }),
  // --- GitHub accounts (GitModel hub instances) ---
  listGithubAccounts: (token: string) =>
    request<GitHubAccount[]>("/github-accounts", token),
  startGithubDevice: (token: string) =>
    request<DeviceStart>("/github-accounts/device/start", token, {
      method: "POST",
    }),
  // device/poll takes account_id as a QUERY param and no body.
  pollGithubDevice: (token: string, accountId: string) =>
    request<DevicePoll>(
      `/github-accounts/device/poll?account_id=${encodeURIComponent(accountId)}`,
      token,
      { method: "POST" },
    ),
  deleteGithubAccount: (token: string, id: string) =>
    requestNoContent(`/github-accounts/${id}`, token, { method: "DELETE" }),
  // --- Deploy config (GitHub PATs + SP push; gates add-account) ---
  getDeployStatus: (token: string) =>
    request<DeployConfigStatus>("/deploy-config/status", token),
  saveDeployPats: (
    token: string,
    body: { bootstrap_pat?: string; deploy_pat?: string },
  ) =>
    request<DeployConfigStatus>("/deploy-config/pats", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  pushSpCreds: (token: string) =>
    request<DeployConfigStatus>("/deploy-config/push-sp", token, {
      method: "POST",
    }),
};
