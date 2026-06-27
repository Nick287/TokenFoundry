import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type UsageTelemetry } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { UsageCard } from "./UsageCard";

// Calls-per-hour bars. CSS-only (no chart lib): each bar's height is scaled to
// the busiest hour; the title attribute carries the exact time + count on hover.
function TrendBars({ data }: { data: UsageTelemetry["by_hour"] }) {
  const max = Math.max(1, ...data.map((d) => d.calls));
  return (
    <div className="trend-bars card">
      {data.map((d) => (
        <div
          key={d.ts}
          className="trend-bar"
          style={{ height: `${Math.max(4, (d.calls / max) * 100)}%` }}
          title={`${new Date(d.ts).toLocaleString()} — ${d.calls}`}
        />
      ))}
    </div>
  );
}

// Admin cross-tenant usage view — pick a tenant from the dropdown.
// Two data sources, shown separately:
//   * Cosmos      → usage & cost summary + per-call log (billing source)
//   * App Insights → call counts & latency (telemetry, sampled)
export function UsageDashboardPage() {
  const principal = usePrincipal()!;
  const { t } = useTranslation();
  const [tenantId, setTenantId] = useState("");

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
  });

  const usage = useQuery({
    queryKey: ["admin-usage", tenantId],
    queryFn: () => api.tenantUsage(principal.token, tenantId),
    enabled: tenantId.length > 0,
  });

  const records = useQuery({
    queryKey: ["admin-usage-records", tenantId],
    queryFn: () => api.tenantUsageRecords(principal.token, tenantId),
    enabled: tenantId.length > 0,
  });

  const telemetry = useQuery({
    queryKey: ["admin-usage-telemetry"],
    queryFn: () => api.usageTelemetry(principal.token),
  });

  return (
    <section>
      <h2>{t("usage.title")}</h2>
      <p className="help-card">{t("help.usage")}</p>
      <div className="card form-row">
        <select value={tenantId} onChange={(e) => setTenantId(e.target.value)}>
          <option value="">{t("usage.selectTenant")}</option>
          {tenants.data?.map((tn) => (
            <option key={tn.id} value={tn.id}>
              {tn.name} ({tn.id})
            </option>
          ))}
        </select>
      </div>
      {!tenantId && <p className="hint">{t("usage.selectPrompt")}</p>}

      {/* --- Block 1: Cosmos (usage & cost + call log) --- */}
      {tenantId && (
        <>
          <h3>{t("usage.cosmosSection")}</h3>
          {usage.isLoading && <p>{t("common.loading")}</p>}
          {usage.isError && <p className="error">{t("usage.loadFailed")}</p>}
          {usage.data && <UsageCard usage={usage.data} />}

          <h4>{t("usage.callLog")}</h4>
          {records.isLoading ? (
            <p>{t("common.loading")}</p>
          ) : records.data && records.data.length > 0 ? (
            <table className="card">
              <thead>
                <tr>
                  <th>{t("usage.colTime")}</th>
                  <th>{t("usage.colModel")}</th>
                  <th>{t("usage.colKey")}</th>
                  <th>{t("usage.colPromptTok")}</th>
                  <th>{t("usage.colCompletionTok")}</th>
                  <th>{t("usage.colCachedTok")}</th>
                </tr>
              </thead>
              <tbody>
                {records.data.map((r, i) => (
                  <tr key={`${r.ts}-${i}`}>
                    <td>{r.ts ? new Date(r.ts).toLocaleString() : "—"}</td>
                    <td>{r.api ?? r.route}</td>
                    <td>
                      <code className="id-cell">{r.subscription ?? "—"}</code>
                    </td>
                    <td>{r.prompt_tok.toLocaleString()}</td>
                    <td>{r.completion_tok.toLocaleString()}</td>
                    <td>{r.cached_tok.toLocaleString()}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="hint">{t("usage.noRecords")}</p>
          )}
        </>
      )}

      {/* --- Block 2: App Insights (calls & latency) --- */}
      <h3>{t("usage.telemetrySection")}</h3>
      {telemetry.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : telemetry.data && telemetry.data.by_api.length > 0 ? (
        <>
          <p className="hint">
            {t("usage.totalCalls")}: {telemetry.data.total_calls.toLocaleString()}
          </p>
          <table className="card">
            <thead>
              <tr>
                <th>{t("usage.colApi")}</th>
                <th>{t("usage.colCalls")}</th>
                <th>{t("usage.colP50")}</th>
                <th>{t("usage.colP95")}</th>
                <th>{t("usage.colGateway")}</th>
                <th>{t("usage.colBackend")}</th>
                <th>{t("usage.colFailures")}</th>
              </tr>
            </thead>
            <tbody>
              {telemetry.data.by_api.map((row) => (
                <tr key={row.name}>
                  <td>{row.name}</td>
                  <td>{row.calls.toLocaleString()}</td>
                  <td>{row.p50 != null ? `${Math.round(row.p50)} ms` : "—"}</td>
                  <td>{row.p95 != null ? `${Math.round(row.p95)} ms` : "—"}</td>
                  <td>
                    {row.gateway_p50 != null
                      ? `${Math.round(row.gateway_p50)} ms`
                      : "—"}
                  </td>
                  <td>
                    {row.backend_p50 != null
                      ? `${Math.round(row.backend_p50)} ms`
                      : "—"}
                  </td>
                  <td>{row.failures.toLocaleString()}</td>
                </tr>
              ))}
            </tbody>
          </table>

          {telemetry.data.by_hour.length > 0 && (
            <>
              <h4>{t("usage.trendSection")}</h4>
              <TrendBars data={telemetry.data.by_hour} />
            </>
          )}
        </>
      ) : (
        <p className="hint">{t("usage.noTelemetry")}</p>
      )}
    </section>
  );
}
