import { useState } from "react";
import { ResponsiveContainer, LineChart, Line, XAxis, YAxis, Tooltip, Legend } from "recharts";
import { getUsage, getPerfHeadline, getPerfTrend } from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";
import { periodToSince, elapsed } from "../utils.js";
import { UsageView } from "./UsageView.jsx";
import { PerformanceTab } from "./PerformanceTab.jsx";

const PERIOD_OPTIONS = [
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "all", label: "All time" },
];

const fmtMoney = (n) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

// recharts renders Tooltip's contentStyle and axis `tick` props as SVG/attribute
// values that can't resolve CSS custom properties, so this literal must be kept
// in sync with tokens.css's --text-xs (11px).
const CHART_TICK_FONT_SIZE = 11;

const CHART_STYLE = {
  contentStyle: {
    background: "var(--panel)",
    border: "1px solid var(--line)",
    color: "var(--txt)",
    fontSize: CHART_TICK_FONT_SIZE,
  },
};

function StatCard({ label, value }) {
  return (
    <div className="perf-stat-card">
      <div className="perf-stat-label">{label}</div>
      <div className="perf-stat-value">{value}</div>
    </div>
  );
}

export function AnalyticsTab({ project }) {
  const [period, setPeriod] = useState("all");
  const since = periodToSince(period);
  const from = since ? since.slice(0, 10) : undefined;

  const { data, loading, forbidden, error, retry } = usePageData(
    () =>
      Promise.all([
        getUsage(since, project.id),
        getPerfHeadline(project.id, { from }),
        getPerfTrend(project.id, { from }),
      ]).then(([usage, headline, trend]) => ({ usage, headline, trend })),
    [project.id, period]
  );

  return (
    <div className="analytics-tab">
      <div className="u-period">
        {PERIOD_OPTIONS.map(({ key, label }) => (
          <button
            key={key}
            className={`tap-target${period === key ? " active" : ""}`}
            onClick={() => setPeriod(key)}
          >
            {label}
          </button>
        ))}
      </div>

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        {data && (
          <>
            <div className="perf-stat-row">
              <StatCard label="Cost This Period" value={fmtMoney(data.usage.total_cost_usd)} />
              <StatCard label="Jobs Run" value={data.headline.total_jobs} />
              <StatCard
                label="Success Rate"
                value={
                  data.headline.success_rate != null
                    ? `${(data.headline.success_rate * 100).toFixed(1)}%`
                    : "—"
                }
              />
              <StatCard
                label="Avg Cycle Time"
                value={
                  data.headline.avg_cycle_time_s != null
                    ? elapsed(data.headline.avg_cycle_time_s)
                    : "—"
                }
              />
            </div>

            {data.headline.excludes_operational &&
              data.headline.operational_jobs_excluded != null && (
                <p className="hint">
                  Jobs Run, Success Rate, and Avg Cycle Time exclude{" "}
                  {data.headline.operational_jobs_excluded} auto-deploy job
                  {data.headline.operational_jobs_excluded === 1 ? "" : "s"}.
                </p>
              )}

            <div className="perf-trends-grid">
              <div className="perf-chart-panel">
                <div className="perf-chart-title">Cost Over Time</div>
                {data.trend.length === 0 ? (
                  <div className="perf-chart-empty">
                    <p className="hint">No cost data yet for this period.</p>
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={data.trend} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                      <XAxis
                        dataKey="date"
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                      />
                      <YAxis
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                        width={48}
                      />
                      <Tooltip {...CHART_STYLE} formatter={(v) => fmtMoney(v)} />
                      <Line
                        type="monotone"
                        dataKey="cost_usd"
                        stroke="var(--accent)"
                        name="Cost"
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>

              <div className="perf-chart-panel">
                <div className="perf-chart-title">Throughput &amp; Success</div>
                {data.trend.length === 0 ? (
                  <div className="perf-chart-empty">
                    <p className="hint">No job outcome data yet for this period.</p>
                  </div>
                ) : (
                  <ResponsiveContainer width="100%" height={220}>
                    <LineChart data={data.trend} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                      <XAxis
                        dataKey="date"
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                      />
                      <YAxis
                        tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                        width={48}
                      />
                      <Tooltip {...CHART_STYLE} />
                      <Legend wrapperStyle={{ color: "var(--dim)", fontSize: "var(--text-xs)" }} />
                      <Line
                        type="monotone"
                        dataKey="jobs_completed"
                        stroke="var(--ok)"
                        name="Completed"
                        dot={false}
                      />
                      <Line
                        type="monotone"
                        dataKey="jobs_failed"
                        stroke="var(--bad)"
                        name="Failed"
                        dot={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                )}
              </div>
            </div>

            <details className="analytics-section">
              <summary>Cost breakdown</summary>
              <UsageView projectId={project.id} />
            </details>

            <details className="analytics-section">
              <summary>Pipeline detail</summary>
              <PerformanceTab projectId={project.id} />
            </details>
          </>
        )}
      </PageState>
    </div>
  );
}
