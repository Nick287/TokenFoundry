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
  monthly_budget_usd: number | null;
  created_at: string;
}

export interface VirtualKeySecret extends VirtualKey {
  key_value: string;
}

export interface ModelRoute {
  id: string;
  name: string;
  provider: "openai" | "anthropic" | "google";
  owner_scope: "PLATFORM" | "TENANT";
  markup_pct: number;
  created_at: string;
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
  route: string;
  api: string | null;
  prompt_tok: number;
  completion_tok: number;
  cached_tok: number;
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
  listRoutes: (token: string) => request<ModelRoute[]>("/routes", token),
  createRoute: (token: string, body: Record<string, unknown>) =>
    request<ModelRoute>("/routes", token, {
      method: "POST",
      body: JSON.stringify(body),
    }),
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
  myUsage: (token: string) => request<UsageSummary>("/usage", token),
  tenantUsage: (token: string, tenantId: string) =>
    request<UsageSummary>(`/admin/usage/${tenantId}`, token),
  tenantUsageRecords: (token: string, tenantId: string) =>
    request<UsageRecordView[]>(`/admin/usage/${tenantId}/records`, token),
  usageTelemetry: (token: string) =>
    request<UsageTelemetry>("/admin/usage-telemetry", token),
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
};
