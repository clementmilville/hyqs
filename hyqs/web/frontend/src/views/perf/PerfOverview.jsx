import { BarChart, Bar, XAxis, YAxis, Tooltip, Legend, ResponsiveContainer } from "recharts";
import { getPerfHeadline, getPerfStageStats, getPerfSlowestJobs } from "../../api.js";
import { usePageData } from "../../hooks/usePageData.js";
import { PageState } from "../../components/PageState.jsx";
import { elapsed } from "../../utils.js";

const STATUS_CLASS = { done: "ok", failed: "bad", running: "run", cancelled: "bad" };

const fmtTok = (n) => {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(0)}K`;
  return String(n);
};

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

export function PerfOverview({ projectId, filters, onSelectJob }) {
  const filtersKey = JSON.stringify(filters);
  const { data, loading, forbidden, error, retry } = usePageData(
    () =>
      Promise.all([
        getPerfHeadline(projectId, filters),
        getPerfStageStats(projectId, filters),
        getPerfSlowestJobs(projectId, filters),
      ]).then(([headline, stages, slowest]) => ({ headline, stages, slowest })),
    [projectId, filtersKey]
  );

  return (
    <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
      {data && (
        <div>
          <div className="perf-stat-row">
            <StatCard label="Total Jobs" value={data.headline.total_jobs} />
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
            <StatCard label="Total Tokens" value={fmtTok(data.headline.total_tokens)} />
            <StatCard label="Total Cost" value={fmtMoney(data.headline.total_cost_usd)} />
          </div>

          <h3 className="section-title">Stage Duration</h3>
          {data.stages.length === 0 ? (
            <p className="hint">No stage data yet for this project.</p>
          ) : (
            <div className="table-scroll">
              <ResponsiveContainer width="100%" height={280}>
                <BarChart data={data.stages} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                  <XAxis
                    dataKey="stage"
                    tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                  />
                  <YAxis
                    tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                    unit="s"
                    width={48}
                  />
                  <Tooltip
                    {...CHART_STYLE}
                    formatter={(v) => (v != null ? `${v.toFixed(1)}s` : "—")}
                  />
                  <Legend wrapperStyle={{ color: "var(--dim)", fontSize: "var(--text-xs)" }} />
                  <Bar dataKey="avg_duration_s" fill="var(--accent)" name="Avg (s)" />
                  <Bar dataKey="p50_duration_s" fill="var(--ok)" name="P50 (s)" />
                  <Bar dataKey="p95_duration_s" fill="var(--bad)" name="P95 (s)" />
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}

          <h3 className="section-title">Slowest Jobs</h3>
          {data.slowest.length === 0 ? (
            <p className="hint">No completed jobs yet.</p>
          ) : (
            <ul className="perf-slowest-list">
              {data.slowest.map((r) => (
                <li key={r.job_id} className="perf-slowest-item">
                  <div className="perf-slowest-row">
                    <span className="perf-slowest-id">#{r.job_id}</span>
                    <span
                      className="perf-slowest-title"
                      onClick={() => onSelectJob && onSelectJob(r)}
                      style={onSelectJob ? { cursor: "pointer" } : {}}
                    >
                      {r.title || r.idea}
                    </span>
                    <span className="perf-slowest-stage">{r.stage}</span>
                    <span className={`badge ${STATUS_CLASS[r.status] || ""}`}>{r.status}</span>
                    <span className="perf-slowest-dur">
                      {r.total_duration_s != null ? elapsed(r.total_duration_s) : "—"}
                    </span>
                  </div>
                  <details className="perf-slowest-meta-details">
                    <summary>stage &amp; duration</summary>
                    <div className="perf-slowest-meta-full">
                      <span>{r.stage}</span>
                      <span>{r.total_duration_s != null ? elapsed(r.total_duration_s) : "—"}</span>
                    </div>
                  </details>
                  {r.idea && (
                    <details className="perf-slowest-idea-details">
                      <summary>idea</summary>
                      <pre className="perf-slowest-idea-full">{r.idea}</pre>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </PageState>
  );
}

function StatCard({ label, value }) {
  return (
    <div className="perf-stat-card">
      <div className="perf-stat-label">{label}</div>
      <div className="perf-stat-value">{value}</div>
    </div>
  );
}
