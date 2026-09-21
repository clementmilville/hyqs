import { useCallback, useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import { Radio } from "lucide-react";
import { getDeploymentReliability, getJobsFiledByBreakdown } from "../api.js";
import { periodToSince } from "../utils.js";
import { PageState } from "../components/PageState.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { UsageView } from "./UsageView.jsx";

const PERIOD_OPTIONS = [
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "all", label: "All time" },
];

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

// Group the flat (source, source_actor, count) rows into one entry per
// source, each carrying its own actor breakdown for the expandable detail.
function groupBySource(breakdown) {
  const bySource = new Map();
  for (const row of breakdown) {
    const key = row.source || "unknown";
    if (!bySource.has(key)) bySource.set(key, { source: key, count: 0, actors: [] });
    const g = bySource.get(key);
    g.count += row.count;
    g.actors.push({ actor: row.source_actor || "(unknown)", count: row.count });
  }
  return Array.from(bySource.values()).sort((a, b) => b.count - a.count);
}

function FiledBySourceItem({ group }) {
  const [open, setOpen] = useState(false);
  return (
    <li className="u-stage-item">
      <div className="u-stage-row clickable" onClick={() => setOpen((o) => !o)}>
        <span className="expand">{open ? "▾" : "▸"}</span>
        <span className="u-stage-source">{group.source}</span>
        <span className="u-stage-cost">
          {group.count} job{group.count === 1 ? "" : "s"}
        </span>
      </div>
      {open && (
        <ul className="u-jobs">
          {group.actors.map((a) => (
            <li key={a.actor} className="u-job">
              <span className="u-name">{a.actor}</span>
              <span className="u-tok">{a.count}</span>
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

function FiledByBreakdown() {
  const [breakdown, setBreakdown] = useState(null);
  const [period, setPeriod] = useState("all");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null); // null | "forbidden" | "error"

  const load = useCallback(() => {
    const since = periodToSince(period);
    return getJobsFiledByBreakdown(since)
      .then((d) => {
        setBreakdown(d);
        setStatus(null);
        setLoading(false);
      })
      .catch((e) => {
        setStatus(e.message === "forbidden" ? "forbidden" : "error");
        setLoading(false);
      });
  }, [period]);

  useEffect(() => {
    setLoading(true);
    setStatus(null);
    load();
  }, [load]);

  function retry() {
    setLoading(true);
    setStatus(null);
    load();
  }

  const groups = breakdown ? groupBySource(breakdown) : [];

  return (
    <>
      <h3 className="section-title">Filed by / channel</h3>
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
      <PageState
        forbidden={status === "forbidden" && !breakdown}
        error={status === "error" && !breakdown}
        loading={loading && !breakdown}
        retry={retry}
      >
        {breakdown &&
          (breakdown.length === 0 ? (
            <EmptyState
              icon={Radio}
              title="No jobs filed yet."
              hint="Jobs filed via the UI, Slack, CLI, MCP, or the intake flow will appear here grouped by channel."
            />
          ) : (
            <>
              <div className="table-scroll">
                <ResponsiveContainer width="100%" height={220}>
                  <BarChart data={groups} margin={{ top: 4, right: 16, left: 0, bottom: 4 }}>
                    <XAxis
                      dataKey="source"
                      tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                    />
                    <YAxis
                      tick={{ fill: "var(--dim)", fontSize: CHART_TICK_FONT_SIZE }}
                      width={40}
                      allowDecimals={false}
                    />
                    <Tooltip {...CHART_STYLE} />
                    <Bar dataKey="count" fill="var(--accent)" name="Jobs" />
                  </BarChart>
                </ResponsiveContainer>
              </div>
              <ul className="u-stage-list">
                {groups.map((g) => (
                  <FiledBySourceItem key={g.source} group={g} />
                ))}
              </ul>
            </>
          ))}
      </PageState>
    </>
  );
}

export function DeploymentReliability() {
  const [projects, setProjects] = useState(null);
  const [period, setPeriod] = useState("all");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);

  const load = useCallback(() => {
    const since = periodToSince(period);
    return getDeploymentReliability(since)
      .then((data) => {
        setProjects(data);
        setStatus(null);
        setLoading(false);
      })
      .catch((error) => {
        setStatus(error.message === "forbidden" ? "forbidden" : "error");
        setLoading(false);
      });
  }, [period]);

  useEffect(() => {
    setLoading(true);
    setStatus(null);
    load();
  }, [load]);

  function retry() {
    setLoading(true);
    setStatus(null);
    load();
  }

  return (
    <>
      <h3 className="section-title">Deployment reliability</h3>
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
      <PageState
        forbidden={status === "forbidden" && !projects}
        error={status === "error" && !projects}
        loading={loading && !projects}
        retry={retry}
      >
        {projects &&
          (projects.length === 0 ? (
            <EmptyState
              icon={Radio}
              title="No deployment reliability data yet."
              hint="Operational auto-deploy jobs will appear here once they reach a terminal state."
            />
          ) : (
            <div className="table-scroll">
              <table className="perf-jobs-table">
                <thead>
                  <tr>
                    <th>Project</th>
                    <th>Total</th>
                    <th>Done</th>
                    <th>Failed</th>
                    <th>Cancelled</th>
                    <th>Failure rate</th>
                  </tr>
                </thead>
                <tbody>
                  {projects.map((project) => (
                    <tr key={project.project_id}>
                      <td>{project.project_name}</td>
                      <td>{project.total}</td>
                      <td>{project.done}</td>
                      <td>{project.failed}</td>
                      <td>{project.cancelled}</td>
                      <td>{(project.failure_rate * 100).toFixed(1)}%</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ))}
      </PageState>
    </>
  );
}

export function AdminUsageCost() {
  return (
    <div className="usage-cost">
      <h3 className="fleet-section-head">Usage & Cost</h3>
      <UsageView />
      <FiledByBreakdown />
      <DeploymentReliability />
      <p className="dim">
        Visitor analytics has moved. See <a href="#admin/visitor-analytics">Visitor analytics</a>.
      </p>
    </div>
  );
}
