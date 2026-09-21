import { useEffect, useRef, useState } from "react";
import { ArrowLeft, ExternalLink, Eye } from "lucide-react";
import { listAgents, cancelJob, retryJob, archiveJob, unarchiveJob } from "../api.js";
import { dur } from "../utils.js";
import { getStatusBadge } from "../components/jobTableShared.jsx";
import { JobDetailTabs } from "../components/JobDetailTabs.jsx";
import { useJobDetailLive } from "../hooks/useJobDetailLive.js";
import { useToast } from "../components/Toast.jsx";
import { GatedAction } from "../context.js";
import { Spinner } from "../components/Spinner.jsx";
import { EmptyState } from "../components/EmptyState.jsx";

const PAGE_STYLE = {
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-3)",
  minWidth: 0,
};

const HEADER_STYLE = {
  position: "sticky",
  top: 0,
  zIndex: "var(--z-dropdown)",
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-2)",
  background: "var(--bg)",
  paddingBottom: "var(--space-2h)",
  borderBottom: "1px solid var(--line)",
};

function earliestOf(values) {
  const present = values.filter(Boolean);
  return present.length ? present.reduce((a, b) => (a < b ? a : b)) : "";
}

function latestOf(values) {
  const present = values.filter(Boolean);
  return present.length ? present.reduce((a, b) => (a > b ? a : b)) : "";
}

function formatAgentLabel(agent) {
  if (!agent) return null;
  const detail = agent.model || agent.provider;
  return detail ? `${agent.name} · ${detail}` : agent.name;
}

function currentExecutorLabel(currentExecutor, agents) {
  if (!currentExecutor) return null;
  if (currentExecutor.kind !== "agent") return currentExecutor.label;

  const rosterAgent = agents.find((agent) => agent.id === currentExecutor.agent_id);
  return (
    formatAgentLabel(rosterAgent) ||
    currentExecutor.label ||
    currentExecutor.provider ||
    null
  );
}

function historicalAgentLabels(events, agents) {
  return [
    ...new Set(
      events
        .map((event) => {
          if (event.agent_name) {
            const detail = event.agent_model || event.agent_provider;
            return detail ? `${event.agent_name} · ${detail}` : event.agent_name;
          }
          return formatAgentLabel(agents.find((agent) => agent.id === event.agent_id));
        })
        .filter(Boolean)
    ),
  ];
}

export function JobDetailPage({ jobId, project, epics, onBack, onOpenJob }) {
  const { job, events, loading, forbidden, error, retry } = useJobDetailLive(jobId);
  const [agents, setAgents] = useState([]);
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const toast = useToast();
  const tabsAnchorRef = useRef(null);

  useEffect(() => {
    if (!project?.id) return;
    let live = true;
    listAgents(project.id)
      .then((a) => live && setAgents(a))
      .catch(() => {});
    return () => {
      live = false;
    };
  }, [project?.id]);

  if (forbidden) {
    return (
      <div style={PAGE_STYLE}>
        <EmptyState title="Access denied" hint="You don't have permission to view this job." />
      </div>
    );
  }

  if (error) {
    return (
      <div style={PAGE_STYLE}>
        <EmptyState title="Unable to load job" hint="Check your connection and try again." />
        <button type="button" className="btn-secondary" onClick={retry}>
          Retry
        </button>
      </div>
    );
  }

  if (loading || !job) {
    return (
      <div style={PAGE_STYLE}>
        <Spinner />
      </div>
    );
  }

  const startedAt = earliestOf(events.map((e) => e.started_at));
  const endedAt = latestOf(events.map((e) => e.ended_at));
  const duration = dur(startedAt, endedAt);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const executorLabel = currentExecutorLabel(job.current_executor, agents);
  const historyLabels = historicalAgentLabels(events, agents);
  const prUrl = lastEvent?.detail?.pr_url || null;

  const epic = job.epic_id != null ? epics.find((e) => e.id === job.epic_id) : null;
  const statusBadge = getStatusBadge(job);
  const canCancel = job.status === "pending" || job.status === "running";
  const canRetry = job.status === "failed" || job.status === "cancelled";
  const isTerminal = ["done", "failed", "cancelled"].includes(job.status);
  const canArchive = isTerminal && !job.archived;
  const canUnarchive = job.archived;

  function scrollToTabs() {
    tabsAnchorRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  async function handleCancel() {
    if (cancelling) return;
    setCancelling(true);
    try {
      await cancelJob(job.id);
      toast.success(`Job #${job.id} cancelled`);
      retry();
    } catch (err) {
      if (err.status !== 409) toast.error(err.message || "Cancel failed");
    } finally {
      setCancelling(false);
    }
  }

  async function handleRetry() {
    if (retrying) return;
    setRetrying(true);
    try {
      await retryJob(job.id);
      toast.success(`Job #${job.id} queued for retry`);
      retry();
    } catch (err) {
      toast.error(err.message || "Retry failed");
    } finally {
      setRetrying(false);
    }
  }

  async function handleArchive() {
    if (archiving) return;
    setArchiving(true);
    try {
      await archiveJob(job.id);
      toast.success(`Job #${job.id} archived`);
      retry();
    } catch (err) {
      toast.error(err.message || "Archive failed");
    } finally {
      setArchiving(false);
    }
  }

  async function handleUnarchive() {
    if (archiving) return;
    setArchiving(true);
    try {
      await unarchiveJob(job.id);
      toast.success(`Job #${job.id} restored`);
      retry();
    } catch (err) {
      toast.error(err.message || "Unarchive failed");
    } finally {
      setArchiving(false);
    }
  }

  return (
    <div style={PAGE_STYLE}>
      <div style={HEADER_STYLE}>
        <button type="button" className="btn-secondary" onClick={onBack}>
          <ArrowLeft size={16} aria-hidden="true" />
          <span>Back to Work</span>
        </button>
        <div className="crumbs">
          {project?.name && <span className="crumb-cur">{project.name}</span>}
          {epic && (
            <>
              <span className="crumb-sep">›</span>
              <button type="button" className="link" onClick={onBack}>
                {epic.name}
              </button>
            </>
          )}
        </div>
        <div
          style={{
            display: "flex",
            alignItems: "center",
            flexWrap: "wrap",
            gap: "var(--space-2h)",
          }}
        >
          <span style={{ color: "var(--dim)", fontSize: "var(--text-sm)" }}>#{job.id}</span>
          <h1 style={{ margin: 0, fontSize: "var(--text-xl)", wordBreak: "break-word" }}>
            {job.title || job.idea}
          </h1>
          <span className={statusBadge.className} title={statusBadge.title}>
            {statusBadge.label}
          </span>
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "var(--space-3)",
            fontSize: "var(--text-xs)",
            color: "var(--dim)",
          }}
        >
          {job.created_at && <span>Created {new Date(job.created_at).toLocaleString()}</span>}
          {startedAt && <span>Started {new Date(startedAt).toLocaleString()}</span>}
          {endedAt && <span>Finished {new Date(endedAt).toLocaleString()}</span>}
          {duration && <span>Duration {duration}</span>}
          {executorLabel && <span>Current executor: {executorLabel}</span>}
          {historyLabels.length > 0 && <span>History: {historyLabels.join(", ")}</span>}
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            alignItems: "center",
            gap: "var(--space-2h)",
          }}
        >
          <GatedAction require="cancel_job">
            {canCancel && (
              <button
                type="button"
                className="cancel-btn"
                disabled={cancelling}
                onClick={handleCancel}
              >
                {cancelling ? "…" : "Cancel"}
              </button>
            )}
          </GatedAction>
          <GatedAction require="retry_job">
            {canRetry && (
              <button type="button" className="retry-btn" disabled={retrying} onClick={handleRetry}>
                {retrying ? "…" : "Retry"}
              </button>
            )}
          </GatedAction>
          <GatedAction require="archive_job">
            {canArchive && (
              <button
                type="button"
                className="archive-btn"
                disabled={archiving}
                onClick={handleArchive}
              >
                {archiving ? "…" : "Archive"}
              </button>
            )}
            {canUnarchive && (
              <button
                type="button"
                className="archive-btn"
                disabled={archiving}
                onClick={handleUnarchive}
              >
                {archiving ? "…" : "Unarchive"}
              </button>
            )}
          </GatedAction>
          <button type="button" className="btn-secondary" onClick={scrollToTabs}>
            <Eye size={16} aria-hidden="true" />
            <span>Watch</span>
          </button>
          {prUrl && (
            <a
              className="btn-secondary"
              style={{ textDecoration: "none" }}
              href={prUrl}
              target="_blank"
              rel="noreferrer"
            >
              <ExternalLink size={16} aria-hidden="true" />
              <span>Open PR</span>
            </a>
          )}
        </div>
      </div>
      <div ref={tabsAnchorRef} />
      <JobDetailTabs job={job} project={project} onOpenJob={onOpenJob} />
    </div>
  );
}
