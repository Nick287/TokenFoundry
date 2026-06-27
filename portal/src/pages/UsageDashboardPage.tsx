import { useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type UsageTelemetry } from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";
import { UsageCard } from "./UsageCard";

// Calls-per-hour mini time series. CSS-only (no chart lib). The backend zero-
// fills every hour in the window, so bars are evenly spaced across a continuous
// 24h timeline; non-zero bars carry their count above, zero hours show a faint
// baseline stub, and the x-axis labels every 4th hour for a time reference.
function TrendBars({ data }: { data: UsageTelemetry["by_hour"] }) {
  const max = Math.max(1, ...data.map((d) => d.calls));
  const fmtHour = (ts: string) =>
    new Date(ts).toLocaleTimeString([], { hour: "2-digit", hour12: false });
  return (
    <div className="trend card">
      <div className="trend-plot">
        {data.map((d) => {
          const pct = d.calls === 0 ? 2 : Math.max(8, (d.calls / max) * 85);
          return (
            <div
              className="trend-col"
              key={d.ts}
              title={`${new Date(d.ts).toLocaleString()} — ${d.calls}`}
            >
              {d.calls > 0 && <span className="trend-val">{d.calls}</span>}
              <div
                className="trend-bar"
                style={{ height: `${pct}%` }}
                data-zero={d.calls === 0 ? "" : undefined}
              />
            </div>
          );
        })}
      </div>
      <div className="trend-axis">
        {data.map((d, i) => (
          <span className="trend-tick" key={d.ts}>
            {i % 4 === 0 ? fmtHour(d.ts) : ""}
          </span>
        ))}
      </div>
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
                      {r.project_name ? (
                        <>
                          {r.project_name}{" "}
                          <code className="id-cell">
                            ({r.subscription ?? "—"})
                          </code>
                        </>
                      ) : (
                        <code className="id-cell">{r.subscription ?? "—"}</code>
                      )}
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
