import { keepPreviousData, useQuery } from "@tanstack/react-query";
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
  // Trim leading empty hours so a single late spike isn't crushed against 20
  // blank bars; keep from the first hour with traffic onward (min 6 cols).
  const first = data.findIndex((d) => d.calls > 0);
  const start = first < 0 ? Math.max(0, data.length - 6) : Math.max(0, Math.min(first - 1, data.length - 6));
  const shown = data.slice(start);
  const max = Math.max(1, ...shown.map((d) => d.calls));
  const fmtHour = (ts: string) =>
    new Date(ts).toLocaleTimeString([], { hour: "2-digit", hour12: false });
  return (
    <div className="trend card">
      <div className="trend-plot">
        {shown.map((d) => {
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
        {shown.map((d, i) => (
          <span className="trend-tick" key={d.ts}>
            {i % 4 === 0 ? fmtHour(d.ts) : ""}
          </span>
        ))}
      </div>
    </div>
  );
}

// Dual-line chart: tokens + calls over time, on ONE plot with two y-scales
// (each series normalized to its own max so both are readable despite very
// different magnitudes). CSS/SVG only, no chart lib. Both series come from the
// same customMetrics buckets so they're aligned. Trims leading empty buckets.
function DualLineChart({
  data,
}: {
  data: Array<{ ts: string; tokens: number; calls: number }>;
}) {
  const { t } = useTranslation();
  const firstTok = data.findIndex((d) => d.tokens > 0 || d.calls > 0);
  const start =
    firstTok < 0
      ? Math.max(0, data.length - 6)
      : Math.max(0, Math.min(firstTok - 1, data.length - 6));
  const shown = data.slice(start);
  if (shown.length === 0) return null;
  const maxTok = Math.max(1, ...shown.map((d) => d.tokens));
  const maxCall = Math.max(1, ...shown.map((d) => d.calls));
  const W = 100; // viewBox width units
  const H = 40; // viewBox height units
  const n = shown.length;
  const x = (i: number) => (n === 1 ? 0 : (i / (n - 1)) * W);
  const yTok = (v: number) => H - (v / maxTok) * (H - 4) - 2;
  const yCall = (v: number) => H - (v / maxCall) * (H - 4) - 2;
  const line = (accessor: (d: (typeof shown)[number]) => number) =>
    shown.map((d, i) => `${i === 0 ? "M" : "L"}${x(i).toFixed(1)},${accessor(d).toFixed(1)}`).join(" ");
  const fmtHour = (ts: string) =>
    new Date(ts).toLocaleTimeString([], { hour: "2-digit", hour12: false });
  const fmtK = (v: number) => (v >= 1000 ? `${(v / 1000).toFixed(1)}k` : `${v}`);
  return (
    <div className="dual-chart card">
      <div className="dual-legend">
        <span className="dual-key dual-key-tokens">■ {t("usage.tokTrendSeries")}</span>
        <span className="dual-key dual-key-calls">■ {t("usage.callTrendSeries")}</span>
      </div>
      <svg className="dual-plot" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none">
        <path className="dual-line-tokens" d={line((d) => yTok(d.tokens))} fill="none" />
        <path className="dual-line-calls" d={line((d) => yCall(d.calls))} fill="none" />
        {shown.map((d, i) => (
          <g key={d.ts}>
            {d.tokens > 0 && <circle className="dual-dot-tokens" cx={x(i)} cy={yTok(d.tokens)} r={0.7} />}
            {d.calls > 0 && <circle className="dual-dot-calls" cx={x(i)} cy={yCall(d.calls)} r={0.7} />}
            <title>{`${new Date(d.ts).toLocaleString()}\n${t("usage.tokTrendSeries")}: ${d.tokens.toLocaleString()}\n${t("usage.callTrendSeries")}: ${d.calls.toLocaleString()}`}</title>
          </g>
        ))}
      </svg>
      <div className="dual-axis">
        {shown.map((d, i) => (
          <span className="dual-tick" key={d.ts}>
            {i % 4 === 0 ? fmtHour(d.ts) : ""}
          </span>
        ))}
      </div>
      <div className="dual-scale">
        <span className="dual-key-tokens">{t("usage.tokTrendSeries")} · max {fmtK(maxTok)}</span>
        <span className="dual-key-calls">{t("usage.callTrendSeries")} · max {maxCall}</span>
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
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(15);
  const [groupBy, setGroupBy] = useState<"model" | "api" | "subscription" | "backend">("model");
  const PAGE_SIZE_OPTIONS = [10, 15, 20];

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
    queryKey: ["admin-usage-records", tenantId, page, pageSize],
    queryFn: () => api.tenantUsageRecords(principal.token, tenantId, page, pageSize),
    enabled: tenantId.length > 0,
    placeholderData: keepPreviousData,
  });

  const telemetry = useQuery({
    queryKey: ["admin-usage-telemetry"],
    queryFn: () => api.usageTelemetry(principal.token),
  });

  const breakdown = useQuery({
    queryKey: ["admin-usage-breakdown", tenantId, groupBy],
    queryFn: () => api.usageBreakdown(principal.token, tenantId, 24, groupBy),
    enabled: tenantId.length > 0,
    placeholderData: keepPreviousData,
  });

  return (
    <section>
      <h2>{t("usage.title")}</h2>
      <p className="help-card">{t("help.usage")}</p>
      <div className="card form-row">
        <select value={tenantId} onChange={(e) => { setTenantId(e.target.value); setPage(1); }}>
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
          ) : records.data && records.data.items.length > 0 ? (
            <>
            <div className="table-scroll">
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
                {records.data.items.map((r, i) => (
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
            </div>
            {(() => {
              const total = records.data.total;
              const pages = Math.max(1, Math.ceil(total / pageSize));
              return (
                <div className="pager">
                  <label className="pager-size">
                    {t("usage.pageSize")}
                    <select
                      value={pageSize}
                      onChange={(e) => {
                        setPageSize(Number(e.target.value));
                        setPage(1);
                      }}
                    >
                      {PAGE_SIZE_OPTIONS.map((n) => (
                        <option key={n} value={n}>
                          {n}
                        </option>
                      ))}
                    </select>
                  </label>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={page <= 1 || records.isFetching}
                    onClick={() => setPage((p) => Math.max(1, p - 1))}
                  >
                    {t("usage.pagePrev")}
                  </button>
                  <span className="pager-info">
                    {t("usage.pageIndicator", { page, pages })}
                  </span>
                  <button
                    type="button"
                    className="btn-sm"
                    disabled={page >= pages || records.isFetching}
                    onClick={() => setPage((p) => p + 1)}
                  >
                    {t("usage.pageNext")}
                  </button>
                </div>
              );
            })()}
            </>
          ) : (
            <p className="hint">{t("usage.noRecords")}</p>
          )}

          {/* --- Token breakdown (App Insights metering): group by model /
                 endpoint / subscription, split by token type, + dual trend. --- */}
          <h3>{t("usage.breakdownSection")}</h3>
          <p className="hint">{t("usage.breakdownHint")}</p>
          <div className="seg-toggle">
            {(["model", "api", "subscription", "backend"] as const).map((g) => (
              <button
                key={g}
                type="button"
                className={groupBy === g ? "seg-btn seg-on" : "seg-btn"}
                onClick={() => setGroupBy(g)}
              >
                {t(`usage.groupBy_${g}`)}
              </button>
            ))}
          </div>
          {breakdown.isLoading ? (
            <p>{t("common.loading")}</p>
          ) : breakdown.data && breakdown.data.groups.length > 0 ? (
            <>
              <div className="stat-row">
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokTotal")}</span>
                  <span className="stat-value">{breakdown.data.totals.total.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokPrompt")}</span>
                  <span className="stat-value">{breakdown.data.totals.prompt.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokCached")}</span>
                  <span className="stat-value">{breakdown.data.totals.cached.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokCompletion")}</span>
                  <span className="stat-value">{breakdown.data.totals.completion.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokReasoning")}</span>
                  <span className="stat-value">{breakdown.data.totals.reasoning.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.tokCacheCreation")}</span>
                  <span className="stat-value">{breakdown.data.totals.cache_creation.toLocaleString()}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.callsLabel")}</span>
                  <span className="stat-value">{breakdown.data.totals.calls.toLocaleString()}</span>
                </div>
              </div>
              <div className="table-scroll">
                <table className="card">
                  <thead>
                    <tr>
                      <th>{t(`usage.groupBy_${breakdown.data.by}`)}</th>
                      <th>{t("usage.tokTotal")}</th>
                      <th>{t("usage.tokPrompt")}</th>
                      <th>{t("usage.tokCached")}</th>
                      <th>{t("usage.tokCompletion")}</th>
                      <th>{t("usage.tokReasoning")}</th>
                      <th>{t("usage.tokCacheCreation")}</th>
                      <th>{t("usage.callsLabel")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdown.data.groups.map((g) => {
                      const label = g.model ?? g.api ?? g.subscription ?? g.backend;
                      return (
                        <tr key={label ?? "unknown"}>
                          <td>
                            {breakdown.data!.by === "subscription" || breakdown.data!.by === "backend" ? (
                              <code className="id-cell">{label || t("usage.modelUnknown")}</code>
                            ) : (
                              label || t("usage.modelUnknown")
                            )}
                          </td>
                          <td>{g.total.toLocaleString()}</td>
                          <td>{g.prompt.toLocaleString()}</td>
                          <td>{g.cached.toLocaleString()}</td>
                          <td>{g.completion.toLocaleString()}</td>
                          <td className={g.reasoning > 0 ? undefined : "cell-zero"}>
                            {g.reasoning.toLocaleString()}
                          </td>
                          <td className={g.cache_creation > 0 ? undefined : "cell-zero"}>
                            {g.cache_creation.toLocaleString()}
                          </td>
                          <td>{g.calls.toLocaleString()}</td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {breakdown.data.trend.some((d) => d.tokens > 0 || d.calls > 0) && (
                <>
                  <h4>{t("usage.tokTrendSection")}</h4>
                  <DualLineChart data={breakdown.data.trend} />
                </>
              )}
            </>
          ) : (
            <p className="hint">{t("usage.noBreakdown")}</p>
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
          <div className="table-scroll">
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
                  <td className={row.p95 != null && row.p95 > 3000 ? "cell-alert" : undefined}>
                    {row.p95 != null ? `${Math.round(row.p95)} ms` : "—"}
                  </td>
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
                  <td className={row.failures > 0 ? "cell-alert" : "cell-zero"}>
                    {row.failures.toLocaleString()}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          </div>

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
