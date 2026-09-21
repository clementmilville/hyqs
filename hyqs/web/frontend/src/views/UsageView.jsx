import { useCallback, useEffect, useState } from "react";
import { getUsage } from "../api.js";
import { periodToSince } from "../utils.js";
import { PageState } from "../components/PageState.jsx";

const fmtTok = (n) =>
  n >= 1e9
    ? `${(n / 1e9).toFixed(2)}B`
    : n >= 1e6
      ? `${(n / 1e6).toFixed(2)}M`
      : n >= 1e3
        ? `${(n / 1e3).toFixed(1)}k`
        : `${n}`;

const fmtMoney = (n) =>
  `$${n.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;

const PERIOD_OPTIONS = [
  { key: "week", label: "Week" },
  { key: "month", label: "Month" },
  { key: "all", label: "All time" },
];

function UsageJob({ job }) {
  return (
    <div className="u-job">
      <span className="u-name" title={`#${job.job_id} ${job.idea}`}>
        <span className="u-jid">#{job.job_id}</span> {job.title || job.idea || "(no description)"}
      </span>
      <span className="u-tok">{fmtTok(job.tokens)}</span>
      <span className="u-cost">{fmtMoney(job.cost_usd)}</span>
    </div>
  );
}

function UsageEpic({ epic }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="u-epic">
      <div className="u-row clickable" onClick={() => setOpen((o) => !o)}>
        <span className="expand">{open ? "▾" : "▸"}</span>
        <span className="u-name">🎯 {epic.name}</span>
        <span className="u-meta">
          {epic.jobs.length} job{epic.jobs.length === 1 ? "" : "s"}
        </span>
        <span className="u-tok">{fmtTok(epic.tokens)}</span>
        <span className="u-cost">{fmtMoney(epic.cost_usd)}</span>
      </div>
      {open && (
        <div className="u-jobs">
          {epic.jobs.map((j) => (
            <UsageJob key={j.job_id} job={j} />
          ))}
        </div>
      )}
    </div>
  );
}

const DEV_SESSION_HINT =
  "Interactive Claude Code sessions run outside the pipeline (via the Stop-hook usage " +
  "collector), so there's no job_id to attribute cost to — hence no per-job breakdown.";

function UsageProject({ project, total }) {
  const isDevSessions = project.project_id === null;
  const expandable = project.epics.length > 0;
  const [open, setOpen] = useState(true);
  const share = total > 0 ? Math.round((project.cost_usd / total) * 100) : 0;
  return (
    <div className="u-project">
      <div
        className={`u-row head${expandable ? " clickable" : ""}`}
        onClick={expandable ? () => setOpen((o) => !o) : undefined}
      >
        <span className="expand">{expandable ? (open ? "▾" : "▸") : ""}</span>
        <span className="u-name" title={isDevSessions ? DEV_SESSION_HINT : undefined}>
          📦 {project.name}
          {isDevSessions && <span className="u-hint-icon"> ⓘ</span>}
        </span>
        <span className="u-meta">{share}%</span>
        <span className="u-tok">{fmtTok(project.tokens)}</span>
        <span className="u-cost">{fmtMoney(project.cost_usd)}</span>
      </div>
      {isDevSessions && <div className="u-subtitle hint">{DEV_SESSION_HINT}</div>}
      {expandable && open && (
        <div className="u-epics">
          {project.epics.map((e) => (
            <UsageEpic key={e.epic_id ?? "none"} epic={e} />
          ))}
        </div>
      )}
    </div>
  );
}

export function UsageView({ projectId } = {}) {
  const [data, setData] = useState(null);
  const [period, setPeriod] = useState("all");
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null); // null | "forbidden" | "error"

  const load = useCallback(() => {
    const since = periodToSince(period);
    return getUsage(since, projectId)
      .then((d) => {
        setData(d);
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
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  function retry() {
    setLoading(true);
    setStatus(null);
    load();
  }

  const projects = data?.projects || [];
  return (
    <div className="usage">
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
        forbidden={status === "forbidden" && !data}
        error={status === "error" && !data}
        loading={loading && !data}
        retry={retry}
      >
        {data && (
          <>
            <div className="stat-grid">
              <div className="perf-stat-card">
                <div className="perf-stat-label">tokens</div>
                <div className="perf-stat-value">{fmtTok(data.total_tokens)}</div>
              </div>
              <div className="perf-stat-card">
                <div className="perf-stat-label">cost</div>
                <div className="perf-stat-value">{fmtMoney(data.total_cost_usd)}</div>
              </div>
            </div>
            {data.excludes_operational && data.operational_jobs_excluded > 0 && (
              <p className="hint">
                Excludes {data.operational_jobs_excluded} operational auto-deploy job
                {data.operational_jobs_excluded === 1 ? "" : "s"}
              </p>
            )}

            <h3 className="section-title">By project → epic → job</h3>
            <div className="table-scroll">
              <div className="u-tree">
                <div className="u-row u-hd">
                  <span className="expand" />
                  <span className="u-name">project / epic / job</span>
                  <span className="u-meta" />
                  <span className="u-tok">tokens</span>
                  <span className="u-cost">cost</span>
                </div>
                {projects.length === 0 && <p className="hint">No usage recorded yet.</p>}
                {projects.map((p) => (
                  <UsageProject
                    key={p.project_id ?? "none"}
                    project={p}
                    total={data.total_cost_usd}
                  />
                ))}
              </div>
            </div>

            <h3 className="section-title">By stage</h3>
            {data.by_source.length === 0 ? (
              <p className="hint">No stage usage recorded yet.</p>
            ) : (
              <ul className="u-stage-list">
                {data.by_source.map((s) => (
                  <li key={s.source} className="u-stage-item">
                    <div className="u-stage-row">
                      <span className="u-stage-source">{s.source}</span>
                      <span className="u-stage-cost">{fmtMoney(s.cost_usd)}</span>
                    </div>
                    <details className="u-stage-details">
                      <summary>
                        {fmtTok(s.tokens)} tokens · {s.runs} run{s.runs === 1 ? "" : "s"}
                      </summary>
                    </details>
                  </li>
                ))}
              </ul>
            )}
          </>
        )}
      </PageState>
    </div>
  );
}
