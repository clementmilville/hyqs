import { useState, useEffect, useRef } from "react";
import { STATUS_CLASS, PRIORITY_LEVELS } from "../constants.js";
import { GatedAction, useRole } from "../context.js";
import {
  cancelJob,
  retryJob,
  archiveJob,
  unarchiveJob,
  setJobPriority,
  getJobDependencies,
  addJobDependency,
  removeJobDependency,
  patchJobTitle,
} from "../api.js";
import { StageFlow } from "./StageFlow.jsx";
import { useToast } from "./Toast.jsx";
import {
  getStatusBadge,
  getSourceInfo,
  handleJobClick,
  SOURCE_COLOR,
  describePriorityReasons,
} from "./jobTableShared.jsx";

export function JobCard({ job, onRefresh, allJobs = [], onOpenJob }) {
  const [cancelling, setCancelling] = useState(false);
  const [retrying, setRetrying] = useState(false);
  const [archiving, setArchiving] = useState(false);
  const [priorityUpdating, setPriorityUpdating] = useState(false);
  const [depPickerOpen, setDepPickerOpen] = useState(false);
  const [depIds, setDepIds] = useState(null);
  const [depFilter, setDepFilter] = useState("");
  const [titleEdit, setTitleEdit] = useState(null);
  const depPickerRef = useRef(null);
  const toast = useToast();
  const { can } = useRole();
  const canCancel = job.status === "pending" || job.status === "running";
  const canRetry = job.status === "failed" || job.status === "cancelled";
  const canEditDeps = !job.archived && job.status !== "done";
  const isTerminal = ["done", "failed", "cancelled"].includes(job.status);
  const canArchive = isTerminal && !job.archived;
  const canUnarchive = job.archived;
  const currentPriorityLabel =
    Object.keys(PRIORITY_LEVELS).find((k) => PRIORITY_LEVELS[k] === job.priority) ?? "Normal";

  const candidateJobs = allJobs.filter((j) => j.project_id === job.project_id && j.id !== job.id);
  const statusBadge = getStatusBadge(job);

  async function openDepPicker(e) {
    e.stopPropagation();
    if (depPickerOpen) {
      setDepPickerOpen(false);
      return;
    }
    try {
      const ids = await getJobDependencies(job.id);
      setDepIds(ids);
    } catch {
      setDepIds([]);
    }
    setDepFilter("");
    setDepPickerOpen(true);
  }

  async function handleTitleBlur() {
    if (titleEdit === null) return;
    const trimmed = titleEdit.trim();
    if (trimmed && trimmed !== (job.title || job.idea)) {
      try {
        await patchJobTitle(job.id, trimmed);
      } catch (err) {
        toast.error(err.message || "Title update failed");
      }
    }
    setTitleEdit(null);
  }

  function handleTitleKeyDown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      e.target.blur();
    } else if (e.key === "Escape") {
      setTitleEdit(null);
    }
  }

  async function handleDepToggle(e, candidateId) {
    e.stopPropagation();
    const checked = e.target.checked;
    try {
      const updated = checked
        ? await addJobDependency(job.id, candidateId)
        : await removeJobDependency(job.id, candidateId);
      setDepIds(updated);
    } catch (err) {
      toast.error(err.message || "Dependency update failed");
    }
  }

  useEffect(() => {
    if (!depPickerOpen) return;
    function onKey(e) {
      if (e.key === "Escape") setDepPickerOpen(false);
    }
    function onOutside(e) {
      if (depPickerRef.current && !depPickerRef.current.contains(e.target)) {
        setDepPickerOpen(false);
      }
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onOutside);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onOutside);
    };
  }, [depPickerOpen]);

  const { sourceLabel, actorLabel, title: sourceTitle } = getSourceInfo(job);

  async function handleCancel(e) {
    e.stopPropagation();
    if (cancelling) return;
    setCancelling(true);
    try {
      await cancelJob(job.id);
      toast.success(`Job #${job.id} cancelled`);
    } catch (err) {
      if (err.status !== 409) {
        toast.error(err.message || "Cancel failed");
      }
    } finally {
      setCancelling(false);
    }
  }

  async function handleRetry(e) {
    e.stopPropagation();
    if (retrying) return;
    setRetrying(true);
    try {
      await retryJob(job.id);
      toast.success(`Job #${job.id} queued for retry`);
      onRefresh?.();
    } catch (err) {
      toast.error(err.message || "Retry failed");
    } finally {
      setRetrying(false);
    }
  }

  async function handleArchive(e) {
    e.stopPropagation();
    if (archiving) return;
    setArchiving(true);
    try {
      await archiveJob(job.id);
      onRefresh?.();
      toast.success(`Job #${job.id} archived`);
    } catch (err) {
      toast.error(err.message || "Archive failed");
    } finally {
      setArchiving(false);
    }
  }

  async function handleUnarchive(e) {
    e.stopPropagation();
    if (archiving) return;
    setArchiving(true);
    try {
      await unarchiveJob(job.id);
      onRefresh?.();
      toast.success(`Job #${job.id} restored`);
    } catch (err) {
      toast.error(err.message || "Unarchive failed");
    } finally {
      setArchiving(false);
    }
  }

  async function handlePriorityChange(e) {
    e.stopPropagation();
    if (priorityUpdating) return;
    const newPriority = parseInt(e.target.value, 10);
    setPriorityUpdating(true);
    try {
      await setJobPriority(job.id, newPriority);
    } catch (_) {
      // SSE will reflect actual state
    } finally {
      setPriorityUpdating(false);
    }
  }

  return (
    <div className={`job ${STATUS_CLASS[job.status] || ""}`}>
      <div className="jobhead clickable" onClick={() => handleJobClick(job, onOpenJob)}>
        <span className="jid">#{job.id}</span>
        {can("edit_job_deps") ? (
          titleEdit !== null ? (
            <input
              className="idea-inline title-edit-input"
              value={titleEdit}
              onChange={(e) => setTitleEdit(e.target.value)}
              onBlur={handleTitleBlur}
              onKeyDown={handleTitleKeyDown}
              onClick={(e) => e.stopPropagation()}
              autoFocus
            />
          ) : (
            <span
              className="idea-inline"
              onClick={(e) => {
                e.stopPropagation();
                setTitleEdit(job.title || job.idea);
              }}
              title="Click to edit title"
            >
              {job.title || job.idea}
            </span>
          )
        ) : (
          <span className="idea-inline">{job.title || job.idea}</span>
        )}
        <span className={statusBadge.className} title={statusBadge.title}>
          {statusBadge.label}
        </span>
        <span
          className={`badge source-badge ${SOURCE_COLOR[sourceLabel] || "source-unknown"}`}
          title={sourceTitle}
        >
          {sourceLabel}
          {actorLabel && (
            <span className="source-actor job-table-source-actor"> · {actorLabel}</span>
          )}
        </span>
        <select
          value={PRIORITY_LEVELS[currentPriorityLabel]}
          onChange={handlePriorityChange}
          onClick={(e) => e.stopPropagation()}
          disabled={priorityUpdating}
          title="Job priority"
        >
          {Object.entries(PRIORITY_LEVELS).map(([label, val]) => (
            <option key={label} value={val}>
              {label}
            </option>
          ))}
        </select>
        {job.effective_priority != null && job.effective_priority !== job.priority && (
          <span className="badge warn" title={describePriorityReasons(job.priority_reasons)}>
            ↑ {job.effective_priority}
          </span>
        )}
        {job.waiting_on?.length > 0 && (
          <span className="waiting-badge">waiting on #{job.waiting_on.join(", #")}</span>
        )}
        <GatedAction require="edit_job_deps">
          {canEditDeps && candidateJobs.length > 0 && (
            <span className="dep-picker-wrap" ref={depPickerRef}>
              <button className="dep-edit-btn" title="Edit dependencies" onClick={openDepPicker}>
                ✏
              </button>
              {depPickerOpen && (
                <div className="dep-picker-dropdown" onClick={(e) => e.stopPropagation()}>
                  <input
                    className="dep-picker-search"
                    placeholder="Filter…"
                    value={depFilter}
                    onChange={(e) => setDepFilter(e.target.value)}
                    onClick={(e) => e.stopPropagation()}
                    autoFocus
                  />
                  <div className="dep-picker-list">
                    {candidateJobs
                      .filter((j) => {
                        const q = depFilter.toLowerCase();
                        return (
                          !q ||
                          String(j.id).includes(q) ||
                          (j.title || j.idea).toLowerCase().includes(q) ||
                          (j.status || "").toLowerCase().includes(q)
                        );
                      })
                      .map((j) => {
                        const candidateBadge = getStatusBadge(j);
                        return (
                          <label key={j.id} className="dep-picker-item" title={j.idea}>
                            <input
                              type="checkbox"
                              checked={depIds != null && depIds.includes(j.id)}
                              onChange={(e) => handleDepToggle(e, j.id)}
                            />
                            <span className="dep-picker-id">#{j.id}</span>
                            <span className="dep-picker-title">{j.title || j.idea}</span>
                            <span className={candidateBadge.className} title={candidateBadge.title}>
                              {candidateBadge.label}
                            </span>
                          </label>
                        );
                      })}
                  </div>
                </div>
              )}
            </span>
          )}
        </GatedAction>
        <GatedAction require="cancel_job">
          {canCancel && (
            <button
              className="cancel-btn"
              disabled={cancelling}
              onClick={handleCancel}
              title="Cancel job"
            >
              {cancelling ? "…" : "Cancel"}
            </button>
          )}
        </GatedAction>
        <GatedAction require="retry_job">
          {canRetry && (
            <button
              className="retry-btn"
              disabled={retrying}
              onClick={handleRetry}
              title="Retry job"
            >
              {retrying ? "…" : "Retry"}
            </button>
          )}
        </GatedAction>
        <GatedAction require="archive_job">
          {canArchive && (
            <button
              className="archive-btn"
              disabled={archiving}
              onClick={handleArchive}
              title="Archive job"
            >
              {archiving ? "…" : "Archive"}
            </button>
          )}
          {canUnarchive && (
            <button
              className="archive-btn"
              disabled={archiving}
              onClick={handleUnarchive}
              title="Unarchive job"
            >
              {archiving ? "…" : "Unarchive"}
            </button>
          )}
        </GatedAction>
      </div>
      <div className="clickable" onClick={() => handleJobClick(job, onOpenJob)}>
        <StageFlow job={job} />
      </div>
      {job.error && <div className="err">{job.error}</div>}
    </div>
  );
}
