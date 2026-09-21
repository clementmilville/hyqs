import { useEffect, useState } from "react";
import { dur } from "../utils.js";
import { STAGE_LABEL } from "../constants.js";
import { MarkdownContent } from "./MarkdownContent.jsx";
import { getJobBacklogSources } from "../api.js";
import { getStatusBadge } from "./jobTableShared.jsx";

const SOURCE_COLOR = {
  ui: "source-ui",
  supervisor: "source-supervisor",
  cli: "source-cli",
  intake: "source-intake",
  mcp: "source-mcp",
  unknown: "source-unknown",
};

export function JobOverviewTab({ job, events }) {
  const [now, setNow] = useState(() => new Date().toISOString());
  const [backlogSources, setBacklogSources] = useState([]);

  useEffect(() => {
    if (job.status !== "running") return;
    const id = setInterval(() => setNow(new Date().toISOString()), 1000);
    return () => clearInterval(id);
  }, [job.status]);

  useEffect(() => {
    getJobBacklogSources(job.id)
      .then(setBacklogSources)
      .catch(() => {});
  }, [job.id]);

  const totalTokens = events.reduce((s, e) => s + (e.tokens || 0), 0);
  const totalCost = events.reduce((s, e) => s + (e.cost_usd || 0), 0);
  const retryCount = events.filter((e) => (e.attempt || 0) > 0).length;

  const elapsed =
    job.status === "running" ? dur(job.started_at, now) : dur(job.started_at, job.ended_at);

  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const resultText = job.error || lastEvent?.summary || null;
  const statusBadge = getStatusBadge(job);

  const sourceLabel = job.source || "unknown";
  const sourceTitle = [
    job.source_actor ? `Filed by ${job.source_actor}` : null,
    `via ${sourceLabel}`,
    job.created_at ? `at ${new Date(job.created_at).toLocaleString()}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  const actorLabel = job.source_actor
    ? job.source_actor.includes("@")
      ? job.source_actor.split("@")[0]
      : job.source_actor
    : null;

  return (
    <div className="job-overview">
      {(job.title || job.idea) && (
        <div className="job-overview-item job-overview-full">
          <label>Summary</label>
          {job.title && <div className="job-overview-summary-title">{job.title}</div>}
          {job.idea && <div className="job-overview-summary-idea">{job.idea}</div>}
        </div>
      )}
      {job.implementation_summary && (
        <div className="job-overview-item job-overview-full">
          <label>What shipped</label>
          <div className="job-overview-implementation">
            <MarkdownContent content={job.implementation_summary} />
          </div>
        </div>
      )}
      <div className="job-overview-item">
        <label>Status</label>
        <div className="job-overview-val">
          <span className={statusBadge.className} title={statusBadge.title}>
            {statusBadge.label}
          </span>
          {job.stage && (
            <span className="job-overview-stage">{STAGE_LABEL[job.stage] || job.stage}</span>
          )}
        </div>
      </div>
      <div className="job-overview-item">
        <label>Filed via</label>
        <div className="job-overview-val">
          <span
            className={`badge source-badge ${SOURCE_COLOR[sourceLabel] || "source-unknown"}`}
            title={sourceTitle}
          >
            {sourceLabel}
            {actorLabel && <span className="source-actor"> · {actorLabel}</span>}
          </span>
        </div>
      </div>
      <div className="job-overview-item">
        <label>Duration</label>
        <div className="job-overview-val">{elapsed || "—"}</div>
      </div>
      <div className="job-overview-item">
        <label>Tokens</label>
        <div className="job-overview-val">
          {totalTokens > 0 ? totalTokens.toLocaleString() : "—"}
        </div>
      </div>
      <div className="job-overview-item">
        <label>Cost</label>
        <div className="job-overview-val">{totalCost > 0 ? `$${totalCost.toFixed(4)}` : "—"}</div>
      </div>
      <div className="job-overview-item">
        <label>Stages</label>
        <div className="job-overview-val">{events.length}</div>
      </div>
      <div className="job-overview-item">
        <label>Retries</label>
        <div className="job-overview-val">{retryCount}</div>
      </div>
      {resultText && (
        <div className="job-overview-item job-overview-full">
          <label>Result</label>
          <div
            className={`job-overview-val job-overview-result${job.error ? " job-overview-result--error" : ""}`}
          >
            {resultText}
          </div>
        </div>
      )}
      {job.source_meta?.chat_session_id && job.project_id && (
        <div className="job-overview-item job-overview-full">
          <a
            href={`/projects/${job.project_id}?session=${encodeURIComponent(job.source_meta.chat_session_id)}`}
            className="job-overview-chat-link"
          >
            View originating conversation
          </a>
        </div>
      )}
      {backlogSources.length > 0 && (
        <div className="job-overview-item job-overview-full">
          <label>From backlog</label>
          <div className="job-overview-val">
            {backlogSources.map((item, i) => (
              <span key={item.id}>
                {i > 0 && ", "}
                <a
                  href={`#workspace/${item.project_id}/plan/backlog`}
                  className="backlog-source-link"
                >
                  {item.title}
                </a>
              </span>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
