import { Fragment, useRef, useState } from "react";
import {
  getProjectsRuntime,
  getProjectRuntime,
  getDeployStatus,
  restartProject,
  stopProject,
  startProject,
} from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { Spinner } from "../components/Spinner.jsx";
import { LogsDrawer } from "../components/LogsDrawer.jsx";
import { GatedAction } from "../context.js";

const RUNTIME_OVERALL_CLASS = { running: "ok", stopped: "bad", partial: "warn" };
const CONTAINER_STATE_CLASS = { running: "ok", exited: "bad", dead: "bad", not_found: "bad" };
const RUNTIME_COL_COUNT = 7;
const DRIFT_COMMAND_RE = /`([^`]+)`/;

function containerStateClass(state) {
  return CONTAINER_STATE_CLASS[String(state || "").toLowerCase()] ?? "warn";
}

function formatPorts(ports) {
  if (!ports || ports.length === 0) return "—";
  return ports.map((p) => `${p.host_port}:${p.container_port}/${p.protocol}`).join(", ");
}

// Puts rows needing attention (unhealthy or drifted) first, matching
// AdminCommandCenter's attention-first lane pattern (§9.6). Native sort is
// stable, so rows within each group keep their incoming order.
function sortRuntimeRows(projects) {
  return [...projects].sort((a, b) => {
    const aAttn = !a.health_ok || a.drift_mismatch ? 0 : 1;
    const bAttn = !b.health_ok || b.drift_mismatch ? 0 : 1;
    return aAttn - bAttn;
  });
}

function DriftFixCommand({ detail }) {
  const [copied, setCopied] = useState(false);
  const [copyError, setCopyError] = useState(false);
  const match = detail.match(DRIFT_COMMAND_RE);
  const command = match ? match[1] : detail;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(command);
      setCopied(true);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  };

  return (
    <div style={{ marginTop: "var(--space-2)" }}>
      <div className="token-reveal-row">
        <code className="token-reveal-secret">{command}</code>
        <button className="btn-secondary" onClick={copy}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      {copyError && <p className="hint">⚠️ Couldn't copy to clipboard</p>}
    </div>
  );
}

// Start/Stop/Restart controls for one project. Hidden entirely (not just
// disabled) without queue_job permission, per §9.4/§8. Restart and Start are
// non-destructive (health-gated redeploy / compose-aware bring-up), so they
// fire directly; Stop takes the app offline, so it's gated behind a
// window.confirm naming the app, per §9.4.
function LifecycleControls({ projectId, projectName, pending, onRestart, onStop, onStart }) {
  const busy = pending != null;
  return (
    <GatedAction require="queue_job">
      <div className="runtime-lifecycle-actions">
        <button
          className="btn-secondary tap-target"
          disabled={busy}
          onClick={() => onRestart(projectId)}
        >
          {pending === "restart" ? "Restarting…" : "Restart"}
        </button>
        <button
          className="btn-secondary tap-target"
          disabled={busy}
          onClick={() => {
            if (window.confirm(`Stop ${projectName}? This will take the app offline.`)) {
              onStop(projectId);
            }
          }}
        >
          {pending === "stop" ? "Stopping…" : "Stop"}
        </button>
        <button
          className="btn-secondary tap-target"
          disabled={busy}
          onClick={() => onStart(projectId)}
        >
          {pending === "start" ? "Starting…" : "Start"}
        </button>
      </div>
    </GatedAction>
  );
}

function RuntimeDetailPanel({
  projectId,
  projectName,
  loading,
  error,
  detail,
  deployStatus,
  pendingAction,
  onRestart,
  onStop,
  onStart,
  onOpenLogs,
}) {
  const containers = detail?.containers ?? [];
  const health = detail?.health ?? {};
  const drift = detail?.drift ?? {};

  return (
    <tr className="runtime-detail-row">
      <td colSpan={RUNTIME_COL_COUNT} className="runtime-detail-cell">
        <div className="runtime-actions-row">
          <LifecycleControls
            projectId={projectId}
            projectName={projectName}
            pending={pendingAction}
            onRestart={onRestart}
            onStop={onStop}
            onStart={onStart}
          />
          {detail && (
            <button className="btn-secondary tap-target" onClick={() => onOpenLogs(projectId)}>
              Logs
            </button>
          )}
        </div>
        {loading ? (
          <Spinner />
        ) : error ? (
          <p className="hint">⚠️ Failed to load: {error}</p>
        ) : (
          <>
            {containers.length === 0 ? (
              <p className="hint">No containers found.</p>
            ) : (
              <div className="table-scroll">
                <table className="sup-table">
                  <thead>
                    <tr>
                      <th>Name</th>
                      <th>State</th>
                      <th>Health</th>
                      <th>Image</th>
                      <th>Ports</th>
                    </tr>
                  </thead>
                  <tbody>
                    {containers.map((c, i) => (
                      <tr key={c.name || c.container_name || i}>
                        <td>{c.name || c.container_name || "—"}</td>
                        <td>
                          <span className={`badge ${containerStateClass(c.state)}`}>
                            {c.state}
                          </span>
                        </td>
                        <td>{c.health || "—"}</td>
                        <td>{c.image_id ? c.image_id.slice(0, 12) : "—"}</td>
                        <td>{formatPorts(c.published_ports)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            <div className="runtime-detail-meta">
              {health.public_url && (
                <span>
                  {health.public_url} → {health.http_status ?? "—"} {health.ok ? "✓" : "✗"}
                </span>
              )}
              {deployStatus && <span>{deployStatus.is_stale ? "stale" : "up to date"}</span>}
              {drift.mismatch && <span>⚠ {drift.detail}</span>}
            </div>
            {drift.mismatch && <DriftFixCommand detail={drift.detail} />}
          </>
        )}
      </td>
    </tr>
  );
}

// Fleet-wide runtime table with an expandable per-project detail panel (jobs
// #1105/#1106/#1110). The fleet roll-up comes from getProjectsRuntime; the
// heavier per-container detail is lazy-fetched via getProjectRuntime on
// first expand and cached (mirrors DecisionsList.jsx), never re-fetched on
// re-expand/collapse.
export function AdminRuntime() {
  const { data, loading, forbidden, error, retry } = usePageData(getProjectsRuntime);
  const projects = data?.projects ?? [];

  const [expandedId, setExpandedId] = useState(null);
  const [loadingId, setLoadingId] = useState(null);
  const [pendingActions, setPendingActions] = useState({});
  const [logsProjectId, setLogsProjectId] = useState(null);
  const cacheRef = useRef(new Map());
  const [, forceRender] = useState(0);

  async function fetchDetail(projectId) {
    setLoadingId(projectId);
    try {
      const detail = await getProjectRuntime(projectId);
      // Deployed-sha staleness is a supplementary metric — its failure
      // shouldn't block rendering the (already successful) runtime detail.
      const deployStatus = await getDeployStatus(projectId).catch(() => null);
      cacheRef.current.set(projectId, { detail, deployStatus });
    } catch (e) {
      cacheRef.current.set(projectId, { error: e.message });
    } finally {
      setLoadingId(null);
      forceRender((n) => n + 1);
    }
  }

  async function handleToggle(projectId) {
    if (expandedId === projectId) {
      setExpandedId(null);
      return;
    }
    setExpandedId(projectId);
    if (cacheRef.current.has(projectId)) return;
    await fetchDetail(projectId);
  }

  async function runLifecycleAction(projectId, action, fn) {
    setPendingActions((prev) => ({ ...prev, [projectId]: action }));
    try {
      await fn(projectId);
    } finally {
      setPendingActions((prev) => {
        const next = { ...prev };
        delete next[projectId];
        return next;
      });
    }
    cacheRef.current.delete(projectId);
    await fetchDetail(projectId);
  }

  const handleRestart = (projectId) => runLifecycleAction(projectId, "restart", restartProject);
  const handleStop = (projectId) => runLifecycleAction(projectId, "stop", stopProject);
  const handleStart = (projectId) => runLifecycleAction(projectId, "start", startProject);

  const logsProject = projects.find((p) => p.project_id === logsProjectId);
  const logsDetail = logsProjectId != null ? cacheRef.current.get(logsProjectId)?.detail : null;
  const logsServices =
    logsDetail?.deploy_mode === "compose" ? (logsDetail.containers ?? []).map((c) => c.name) : null;

  return (
    <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
      {projects.length === 0 ? (
        <EmptyState
          icon="🖥️"
          title="No runtime data"
          hint="Runtime status appears once a project is deployed."
        />
      ) : (
        <div className="table-scroll">
          <table className="sup-table">
            <thead>
              <tr>
                <th>Project</th>
                <th>Deploy Mode</th>
                <th>Overall</th>
                <th>Health</th>
                <th>Drift</th>
                <th>Port</th>
                <th>Containers</th>
              </tr>
            </thead>
            <tbody>
              {sortRuntimeRows(projects).map((p) => {
                const isExpanded = expandedId === p.project_id;
                const cached = cacheRef.current.get(p.project_id);
                return (
                  <Fragment key={p.project_id}>
                    <tr
                      className={`runtime-row${isExpanded ? " expanded" : ""}`}
                      onClick={() => handleToggle(p.project_id)}
                    >
                      <td>{p.name}</td>
                      <td>{p.deploy_mode}</td>
                      <td>
                        <span className={`badge ${RUNTIME_OVERALL_CLASS[p.overall] ?? ""}`}>
                          {p.overall}
                        </span>
                      </td>
                      <td>{p.health_ok ? "✓" : "✗"}</td>
                      <td>
                        {p.drift_mismatch ? (
                          <span className="badge bad" title="Port config drift detected">
                            ⚠ drift
                          </span>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>{p.configured_port ?? "—"}</td>
                      <td>
                        {p.running_count}/{p.container_count}
                      </td>
                    </tr>
                    {isExpanded && (
                      <RuntimeDetailPanel
                        projectId={p.project_id}
                        projectName={p.name}
                        loading={loadingId === p.project_id}
                        error={cached?.error}
                        detail={cached?.detail}
                        deployStatus={cached?.deployStatus}
                        pendingAction={pendingActions[p.project_id]}
                        onRestart={handleRestart}
                        onStop={handleStop}
                        onStart={handleStart}
                        onOpenLogs={setLogsProjectId}
                      />
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {logsProjectId != null && (
        <LogsDrawer
          projectId={logsProjectId}
          projectName={logsProject?.name ?? ""}
          services={logsServices}
          onClose={() => setLogsProjectId(null)}
        />
      )}
    </PageState>
  );
}
