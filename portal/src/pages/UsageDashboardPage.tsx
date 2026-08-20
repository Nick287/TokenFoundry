import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type TokenGroup,
  type TrendBucket,
  type UsageBreakdown,
  type UsageTelemetry,
} from "../api/client";
import { usePrincipal } from "../auth/AuthProvider";

// Selectable time windows, in hours. Capped at 30d because Cosmos expires usage
// documents at 90d and App Insights retention is shorter still — offering a year
// would promise data that no longer exists.
//
// The default is 7 days, not 24 hours. A 24h default is the conventional choice
// and it is wrong here: this dashboard is read for billing, traffic is bursty,
// and a window that goes empty overnight makes a working page look broken. That
// is not hypothetical — it is exactly how this control came to be added.
const WINDOWS = [1, 6, 24, 72, 168, 720] as const;
const DEFAULT_WINDOW = 168;

// The per-type token columns, declared once and rendered by both the stat row
// and the table. Adding a token type upstream should be one edit here, not two
// hand-synchronised JSX blocks that silently drift apart.
//
// Order is billing-narrative: what went in, what was reused from cache, what was
// written to cache, what came out.
const TOKEN_COLS = [
  { key: "prompt_tok", label: "usage.tokPrompt" },
  { key: "cached_tok", label: "usage.tokCached" },
  { key: "cache_write_tok", label: "usage.tokCacheWrite" },
  { key: "completion_tok", label: "usage.tokCompletion" },
] as const satisfies ReadonlyArray<{ key: keyof TokenGroup; label: string }>;

// One chip per HTTP status, biggest first. Rendered under both failure counts so
// the Cosmos figure and the gateway figure can be compared code by code — they
// are NOT expected to match, and the shape of the difference is the point:
// Cosmos only holds calls that reached a hub, so a 503 shed by the circuit
// breaker appears on the gateway side and nowhere else.
function StatusChips({ counts }: { counts: Array<[string, number]> }) {
  if (counts.length === 0) return null;
  const sorted = [...counts].sort((a, b) => b[1] - a[1]);
  return (
    <div className="status-chips">
      {sorted.map(([status, n]) => (
        <span key={status} className="status-chip">
          <code>{status}</code>
          <span className="status-chip-n">{n.toLocaleString()}</span>
        </span>
      ))}
    </div>
  );
}

// Axis label for one trend point. Hourly buckets get a time, daily buckets get a
// date — showing "00" for every point of a 30-day series would be unreadable.
function fmtBucket(ts: string, bucket: TrendBucket): string {
  const d = new Date(ts);
  return bucket === "day"
    ? d.toLocaleDateString([], { month: "numeric", day: "numeric" })
    : d.toLocaleTimeString([], { hour: "2-digit", hour12: false });
}

// Costs run small — a cheap model's hourly spend is fractions of a cent — so a
// fixed 2dp would render most real rows as "$0.00" and look broken. Show enough
// significant digits that a non-zero cost never displays as zero.
function fmtUsd(v: number): string {
  if (!v) return "$0";
  if (v < 0.01) return `$${v.toFixed(6)}`;
  return `$${v.toFixed(4)}`;
}

// Calls-per-bucket mini time series. CSS-only (no chart lib). The backend zero-
// fills every bucket in the window, so bars are evenly spaced across a
// continuous timeline; non-zero bars carry their count above, zero buckets show
// a faint baseline stub, and the x-axis labels every 4th bucket for reference.
function TrendBars({
  data,
  bucket,
}: {
  data: UsageTelemetry["by_hour"];
  bucket: TrendBucket;
}) {
  // Trim leading empty buckets so a single late spike isn't crushed against 20
  // blank bars; keep from the first bucket with traffic onward (min 6 cols).
  const first = data.findIndex((d) => d.calls > 0);
  const start = first < 0 ? Math.max(0, data.length - 6) : Math.max(0, Math.min(first - 1, data.length - 6));
  const shown = data.slice(start);
  const max = Math.max(1, ...shown.map((d) => d.calls));
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
            {i % 4 === 0 ? fmtBucket(d.ts, bucket) : ""}
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
  bucket,
}: {
  data: Array<{ ts: string; tokens: number; calls: number }>;
  bucket: TrendBucket;
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
            {i % 4 === 0 ? fmtBucket(d.ts, bucket) : ""}
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
  const [groupBy, setGroupBy] = useState<UsageBreakdown["by"]>("model");
  // One window governs every block on the page. Splitting it was the previous
  // behaviour and it produced a contradiction: an unfiltered call log sat above
  // a 24h-windowed breakdown, so a page could show 96 calls and "no usage in
  // this window" at the same time.
  const [hours, setHours] = useState<number>(DEFAULT_WINDOW);
  const PAGE_SIZE_OPTIONS = [10, 15, 20];

  const tenants = useQuery({
    queryKey: ["tenants"],
    queryFn: () => api.listTenants(principal.token),
  });

  const records = useQuery({
    queryKey: ["admin-usage-records", tenantId, page, pageSize, hours],
    queryFn: () =>
      api.tenantUsageRecords(principal.token, tenantId, page, pageSize, hours),
    enabled: tenantId.length > 0,
    placeholderData: keepPreviousData,
  });

  const telemetry = useQuery({
    queryKey: ["admin-usage-telemetry", hours],
    queryFn: () => api.usageTelemetry(principal.token, hours),
  });

  const breakdown = useQuery({
    queryKey: ["admin-usage-breakdown", tenantId, groupBy, hours],
    queryFn: () => api.usageBreakdown(principal.token, tenantId, hours, groupBy),
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
        <label className="window-picker">
          {t("usage.window")}
          <select
            value={hours}
            onChange={(e) => {
              setHours(Number(e.target.value));
              // A narrower window shrinks the result set, so page 4 of the old
              // window is usually past the end of the new one.
              setPage(1);
            }}
          >
            {WINDOWS.map((h) => (
              <option key={h} value={h}>
                {t(`usage.window_${h}`)}
              </option>
            ))}
          </select>
        </label>
      </div>
      {!tenantId && <p className="hint">{t("usage.selectPrompt")}</p>}

      {/* --- Block 1: billing (token & cost breakdown) ---
             This is the money view, so it leads. The per-call log used to sit
             here; it is detail, not headline, and now lives at the bottom. --- */}
      {tenantId && (
        <>
          {/* --- Token breakdown (App Insights metering): group by model /
                 endpoint / subscription, split by token type, + dual trend. --- */}
          <h3>{t("usage.breakdownSection")}</h3>
          <p className="hint">{t("usage.breakdownHint")}</p>
          <div className="seg-toggle">
            {(["model", "api", "subscription", "backend", "end_user"] as const).map((g) => (
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
                  <span className="stat-label">{t("usage.colBilled")}</span>
                  <span className="stat-value">{fmtUsd(breakdown.data.totals.billed_usd)}</span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.colCost")}</span>
                  <span className="stat-value">{fmtUsd(breakdown.data.totals.cost_usd)}</span>
                </div>
                {TOKEN_COLS.map((c) => (
                  <div className="stat card" key={c.key}>
                    <span className="stat-label">{t(c.label)}</span>
                    <span className="stat-value">
                      {breakdown.data!.totals[c.key].toLocaleString()}
                    </span>
                  </div>
                ))}
                <div className="stat card">
                  <span className="stat-label">{t("usage.callsLabel")}</span>
                  <span className="stat-value">{breakdown.data.totals.calls.toLocaleString()}</span>
                </div>
                {/* Split out because a raw call count hides upstream rejections:
                    they cost $0, so the money reads correct while the traffic
                    picture is not. 46 of them were invisible on dev-16. */}
                <div className="stat card">
                  <span className="stat-label">{t("usage.okCallsLabel")}</span>
                  <span className="stat-value">
                    {breakdown.data.totals.ok_calls.toLocaleString()}
                  </span>
                </div>
                <div className="stat card">
                  <span className="stat-label">{t("usage.failedCallsLabel")}</span>
                  <span
                    className={
                      breakdown.data.totals.failed_calls > 0
                        ? "stat-value cell-alert"
                        : "stat-value cell-zero"
                    }
                  >
                    {breakdown.data.totals.failed_calls.toLocaleString()}
                  </span>
                  <StatusChips
                    counts={Object.entries(breakdown.data.totals.failed_by_status ?? {})}
                  />
                </div>
              </div>
              <p className="hint">{t("usage.failedHint")}</p>
              <div className="table-scroll">
                <table className="card">
                  <thead>
                    <tr>
                      <th>{t(`usage.groupBy_${breakdown.data.by}`)}</th>
                      <th>{t("usage.colBilled")}</th>
                      <th>{t("usage.colCost")}</th>
                      {TOKEN_COLS.map((c) => (
                        <th key={c.key}>{t(c.label)}</th>
                      ))}
                      <th>{t("usage.callsLabel")}</th>
                      <th>{t("usage.okCallsLabel")}</th>
                      <th>{t("usage.failedCallsLabel")}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {breakdown.data.groups.map((g) => {
                      const label =
                        g.model ?? g.api ?? g.subscription ?? g.backend ?? g.end_user;
                      // Opaque identifiers (key ids, hub ids, customer-supplied
                      // user ids) get monospace treatment; model/api names are prose.
                      const isId =
                        breakdown.data!.by === "subscription" ||
                        breakdown.data!.by === "backend" ||
                        breakdown.data!.by === "end_user";
                      return (
                        <tr key={label ?? "unknown"}>
                          <td>
                            {isId ? (
                              <>
                                <code className="id-cell">
                                  {label || t("usage.modelUnknown")}
                                </code>
                                {/* The id is the identity; the login is the
                                    answer to "which account is this?". Shown
                                    only when the server resolved one — never
                                    invented, so a blank here means the account
                                    is gone, not that it is unnamed. */}
                                {g.label && <span className="muted"> ({g.label})</span>}
                              </>
                            ) : (
                              label || t("usage.modelUnknown")
                            )}
                          </td>
                          <td>{fmtUsd(g.billed_usd)}</td>
                          <td className={g.cost_usd > 0 ? undefined : "cell-zero"}>
                            {fmtUsd(g.cost_usd)}
                          </td>
                          {TOKEN_COLS.map((c) => (
                            <td key={c.key} className={g[c.key] > 0 ? undefined : "cell-zero"}>
                              {g[c.key].toLocaleString()}
                            </td>
                          ))}
                          <td>{g.calls.toLocaleString()}</td>
                          <td>{g.ok_calls.toLocaleString()}</td>
                          <td className={g.failed_calls > 0 ? "cell-alert" : "cell-zero"}>
                            {g.failed_calls.toLocaleString()}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              {breakdown.data.trend.some((d) => d.tokens > 0 || d.calls > 0) && (
                <>
                  <h4>{t("usage.tokTrendSection")}</h4>
                  <DualLineChart
                    data={breakdown.data.trend}
                    bucket={breakdown.data.bucket}
                  />
                </>
              )}
            </>
          ) : (
            <p className="hint">
              {t("usage.noBreakdown")} {t("usage.windowHint")}
            </p>
          )}
        </>
      )}

      {/* --- Block 2: App Insights (calls & latency) --- */}
      <h3>{t("usage.telemetrySection")}</h3>
      {telemetry.isLoading ? (
        <p>{t("common.loading")}</p>
      ) : telemetry.data && telemetry.data.by_api.length > 0 ? (
        <>
          {/* Mirrors the billing block's calls/succeeded/failed row on purpose,
              so the two sources can be read side by side. They count different
              populations — see the hint below — and the difference is the
              interesting part, not a defect. */}
          <div className="stat-row">
            <div className="stat card">
              <span className="stat-label">{t("usage.callsLabel")}</span>
              <span className="stat-value">
                {telemetry.data.total_calls.toLocaleString()}
              </span>
            </div>
            <div className="stat card">
              <span className="stat-label">{t("usage.okCallsLabel")}</span>
              <span className="stat-value">
                {telemetry.data.total_ok.toLocaleString()}
              </span>
            </div>
            <div className="stat card">
              <span className="stat-label">{t("usage.failedCallsLabel")}</span>
              <span
                className={
                  telemetry.data.total_failures > 0
                    ? "stat-value cell-alert"
                    : "stat-value cell-zero"
                }
              >
                {telemetry.data.total_failures.toLocaleString()}
              </span>
              <StatusChips
                counts={(telemetry.data.by_status ?? [])
                  .filter((s) => !s.status.startsWith("2"))
                  .map((s) => [s.status, s.calls] as [string, number])}
              />
            </div>
          </div>
          <p className="hint">{t("usage.telemetryReconcileHint")}</p>
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
              <TrendBars data={telemetry.data.by_hour} bucket={telemetry.data.bucket} />
            </>
          )}
        </>
      ) : (
        <p className="hint">
          {t("usage.noTelemetry")} {t("usage.windowHint")}
        </p>
      )}

      {/* --- Block 3: the per-call log ---
             Last on the page by design: it is the drill-down you reach for
             after the totals above raise a question, and at 200 rows a page it
             would otherwise push every summary below the fold. --- */}
      {tenantId && (
        <>
          <h3>{t("usage.callLog")}</h3>
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
                  <th>{t("usage.colStatus")}</th>
                  <th>{t("usage.colPromptTok")}</th>
                  <th>{t("usage.colCompletionTok")}</th>
                  <th>{t("usage.colCachedTok")}</th>
                  <th>{t("usage.colCacheWriteTok")}</th>
                  <th>{t("usage.colCostUsd")}</th>
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
                    {/* A record written before `status` existed carries null;
                        show a dash rather than inventing a success. */}
                    <td
                      className={
                        r.status == null
                          ? undefined
                          : r.status >= 400
                            ? "cell-alert"
                            : "cell-zero"
                      }
                    >
                      {r.status ?? "—"}
                    </td>
                    <td>{r.prompt_tok.toLocaleString()}</td>
                    <td>{r.completion_tok.toLocaleString()}</td>
                    <td>{r.cached_tok.toLocaleString()}</td>
                    <td>{r.cache_write_tok.toLocaleString()}</td>
                    {/* Cost carries its own provenance. An "unpriced" row is
                        also $0.00, and rendering the two identically would turn
                        "we could not price this" into "this was free" — the
                        importer deliberately refuses to guess a price, and that
                        refusal only helps if it stays visible here. Estimated
                        token counts get the same treatment: they must not read
                        like measured ones. */}
                    <td className={r.cost_source === "unpriced" ? "cell-alert" : undefined}>
                      {r.cost_source === "unpriced" ? (
                        <span title={t("usage.unpricedHint")}>
                          — <small>{t("usage.unpriced")}</small>
                        </span>
                      ) : (
                        <>
                          ${r.billed_usd.toFixed(4)}
                          {r.estimated && (
                            <>
                              {" "}
                              <small
                                className="cell-alert"
                                title={t("usage.estimatedHint")}
                              >
                                {t("usage.estimated")}
                              </small>
                            </>
                          )}
                        </>
                      )}
                    </td>
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
            <p className="hint">
              {t("usage.noRecords")} {t("usage.windowHint")}
            </p>
          )}
        </>
      )}
    </section>
  );
}
