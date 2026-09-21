import { useState } from "react";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "./PageState.jsx";
import { useRole } from "../context.js";
import { useToast } from "./Toast.jsx";
import { REQUEUE_STAGES, STAGE_LABEL } from "../constants.js";
import {
  getJobSupervisorEvents,
  getJobDependents,
  requeueJobAtStage,
  fileFixForwardJob,
  patchJobIdea,
  resolveJob,
  retryJob,
  archiveJob,
  createJob,
} from "../api.js";

// e.detail is JSON-parsed by the store into {diagnosis, action, confidence,
// valid} for "ai_diagnosed" rows (see incident_analyst.py / supervisor.py);
// everything else on this per-job feed is janitor bookkeeping we don't show
// here. Most recent diagnosis wins.
function latestDiagnosis(events) {
  for (let i = events.length - 1; i >= 0; i--) {
    if (events[i].action === "ai_diagnosed") return events[i];
  }
  return null;
}

// Guided resolution for a FAILED job: full failure reason, the supervisor's
// incident-analyst diagnosis (if one ran), and the deterministic actions a
// human can take from here (CONVENTIONS.md §9 — no new top-level tab; this
// lives inside the existing job detail Overview tab).
export function JobResolutionPanel({ job }) {
  const { can } = useRole();
  const toast = useToast();
  const {
    data: supEvents,
    loading,
    forbidden,
    error,
    retry,
  } = usePageData(() => getJobSupervisorEvents(job.id), [job.id]);
  const {
    data: dependents,
    loading: depsLoading,
    forbidden: depsForbidden,
    error: depsError,
    retry: depsRetry,
  } = usePageData(() => getJobDependents(job.id), [job.id]);

  const [stage, setStage] = useState(null);
  const [fixIdea, setFixIdea] = useState(null);
  const [editingIdea, setEditingIdea] = useState(null);
  const [archiveConfirming, setArchiveConfirming] = useState(false);
  const [busy, setBusy] = useState(null);
  const [uncheckedDependentIds, setUncheckedDependentIds] = useState(() => new Set());
  const [conflict, setConflict] = useState(null);

  if (job.status !== "failed") return null;

  const dependentList = dependents || [];
  const checkedDependentIds = dependentList
    .map((d) => d.id)
    .filter((id) => !uncheckedDependentIds.has(id));

  function toggleDependent(id) {
    setUncheckedDependentIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  const latest = latestDiagnosis(supEvents || []);
  const detail = latest?.detail || {};
  const recommended = detail.action || null;
  const isEscalate = recommended?.type === "escalate";
  const recommendedStage = recommended?.type === "requeue_at_stage" ? recommended.stage : null;

  const selectedStage = stage ?? recommendedStage ?? REQUEUE_STAGES[0];
  const failureText = job.failure || job.error || "";
  const defaultFixIdea = fixIdea ?? [failureText, detail.diagnosis].filter(Boolean).join("\n\n");

  async function handleRequeue() {
    setBusy("requeue");
    try {
      await requeueJobAtStage(job.id, selectedStage);
      toast.success(`Job #${job.id} requeued at ${STAGE_LABEL[selectedStage] || selectedStage}`);
    } catch (err) {
      toast.error(err.message || "Requeue failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleFixForward(override = false) {
    const idea = defaultFixIdea.trim();
    if (!idea) {
      toast.error("Describe the fix before filing a job");
      return;
    }
    setBusy("fix-forward");
    try {
      const resp = await fileFixForwardJob(job.id, idea, undefined, checkedDependentIds, override);
      toast.success(`Filed fix job #${resp.job.id}`);
      setConflict(null);
    } catch (err) {
      if (err.status === 409 && err.body?.active_remediation_job_id) {
        setConflict({ action: "fix-forward", message: err.message });
      } else {
        toast.error(err.message || "Filing fix job failed");
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleRetryWithEdit(override = false) {
    const idea = (editingIdea ?? job.idea ?? "").trim();
    if (!idea) {
      toast.error("Idea can't be empty");
      return;
    }
    setBusy("retry");
    try {
      if (idea !== job.idea) await patchJobIdea(job.id, idea);
      await retryJob(job.id, { overrideActiveRemediation: override });
      toast.success(`Job #${job.id} queued for retry`);
      setEditingIdea(null);
      setConflict(null);
    } catch (err) {
      if (err.status === 409 && err.body?.active_remediation_job_id) {
        setConflict({ action: "retry", message: err.message });
      } else {
        toast.error(err.message || "Retry failed");
      }
    } finally {
      setBusy(null);
    }
  }

  async function handleRefile() {
    const idea = (editingIdea ?? job.idea ?? "").trim();
    if (!idea) {
      toast.error("Idea can't be empty");
      return;
    }
    setBusy("refile");
    try {
      const resp = await createJob(idea, job.repo_path, job.epic_id);
      toast.success(`Filed job #${resp.job.id}`);
      setEditingIdea(null);
    } catch (err) {
      toast.error(err.message || "Refile failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleArchive() {
    if (!window.confirm(`Archive job #${job.id}? This retires it without resolving it.`)) return;
    setBusy("archive");
    try {
      await archiveJob(job.id);
      toast.success(`Job #${job.id} archived`);
      setArchiveConfirming(false);
    } catch (err) {
      toast.error(err.message || "Archive failed");
    } finally {
      setBusy(null);
    }
  }

  async function handleResolve() {
    setBusy("resolve");
    try {
      await resolveJob(job.id);
      toast.success(`Job #${job.id} marked resolved`);
    } catch (err) {
      toast.error(err.message || "Mark resolved failed");
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="job-resolution">
      <div className="job-resolution-section">
        <label>Why it failed</label>
        {failureText ? (
          <details className="job-resolution-failure">
            <summary>{failureText.split("\n")[0].slice(0, 140)}</summary>
            <pre className="job-resolution-failure-full">{failureText}</pre>
          </details>
        ) : (
          <p className="hint">No failure detail recorded.</p>
        )}
      </div>

      <div className="job-resolution-section">
        <label>Supervisor diagnosis</label>
        <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
          {latest ? (
            <div className="job-resolution-diagnosis">
              <p>{detail.diagnosis || "No diagnosis text."}</p>
              <div className="job-resolution-diagnosis-meta">
                <span className="badge">{recommended?.type || "no action"}</span>
                {typeof detail.confidence === "number" && (
                  <span className="job-resolution-confidence">
                    {Math.round(detail.confidence * 100)}% confidence
                  </span>
                )}
              </div>
            </div>
          ) : (
            <p className="hint">No supervisor diagnosis yet for this job.</p>
          )}
        </PageState>
      </div>

      <div className="job-resolution-section">
        <label>Resolution options</label>
        <div className="job-resolution-options">
          {conflict && (
            <div className="job-resolution-conflict">
              <p>{conflict.message}</p>
              <div className="job-resolution-option-row">
                <button
                  className="btn-secondary"
                  onClick={() =>
                    conflict.action === "fix-forward"
                      ? handleFixForward(true)
                      : handleRetryWithEdit(true)
                  }
                >
                  Override and proceed
                </button>
                <button className="btn-secondary" onClick={() => setConflict(null)}>
                  Cancel
                </button>
              </div>
            </div>
          )}

          {can("resolve_job") && (
            <div className="job-resolution-option">
              <p className="job-resolution-option-hint">
                Use this when the failure looks transient or environmental — a re-run from a
                checkpoint should succeed.
              </p>
              <div className="job-resolution-option-row">
                <select value={selectedStage} onChange={(e) => setStage(e.target.value)}>
                  {REQUEUE_STAGES.map((s) => (
                    <option key={s} value={s}>
                      {(STAGE_LABEL[s] || s) + (s === recommendedStage ? " (recommended)" : "")}
                    </option>
                  ))}
                </select>
                <button
                  className={isEscalate ? "btn-secondary" : undefined}
                  disabled={busy === "requeue"}
                  onClick={handleRequeue}
                >
                  {busy === "requeue" ? "…" : "Requeue"}
                </button>
              </div>
            </div>
          )}

          {can("resolve_job") && (
            <div className="job-resolution-option">
              <p className="job-resolution-option-hint">
                Use this when the root cause needs its own pipeline job — e.g. a security rejection,
                where a blind requeue just re-fails the gate.
                {isEscalate && (
                  <span className="badge job-resolution-recommended-badge">Recommended</span>
                )}
              </p>
              <textarea
                className="job-resolution-fix-idea"
                value={defaultFixIdea}
                onChange={(e) => setFixIdea(e.target.value)}
                rows={3}
              />
              {(depsLoading || depsForbidden || depsError || dependentList.length > 0) && (
                <div className="job-resolution-repoint">
                  <PageState
                    forbidden={depsForbidden}
                    error={depsError}
                    loading={depsLoading}
                    retry={depsRetry}
                  >
                    {dependentList.length > 0 && (
                      <>
                        <p className="job-resolution-option-hint">
                          {dependentList.length} dependent job(s) will be re-pointed to the new job
                        </p>
                        {dependentList.map((d) => (
                          <label key={d.id} className="job-resolution-repoint-item">
                            <input
                              type="checkbox"
                              checked={!uncheckedDependentIds.has(d.id)}
                              onChange={() => toggleDependent(d.id)}
                            />
                            #{d.id} {d.title}
                          </label>
                        ))}
                      </>
                    )}
                  </PageState>
                </div>
              )}
              <button
                disabled={busy === "fix-forward" || depsError || depsForbidden}
                onClick={() => handleFixForward()}
              >
                {busy === "fix-forward" ? "…" : "File a fix-forward job"}
              </button>
            </div>
          )}

          {can("edit_job_deps") && (
            <div className="job-resolution-option">
              <p className="job-resolution-option-hint">
                {job.needs_split
                  ? "This job was parked because its plan was too large to split automatically. " +
                    "Edit the idea and refile it as a new job — the parked job stays archived."
                  : "Use this for a small mistake in the job description — edit it and retry from the " +
                    "top."}
              </p>
              <textarea
                className="job-resolution-idea-edit"
                value={editingIdea ?? job.idea ?? ""}
                onChange={(e) => setEditingIdea(e.target.value)}
                rows={2}
              />
              {job.needs_split ? (
                <button
                  className="btn-secondary"
                  disabled={busy === "refile"}
                  onClick={handleRefile}
                >
                  {busy === "refile" ? "…" : "Refile idea"}
                </button>
              ) : (
                <button
                  className="btn-secondary"
                  disabled={busy === "retry"}
                  onClick={() => handleRetryWithEdit()}
                >
                  {busy === "retry" ? "…" : "Save idea & retry"}
                </button>
              )}
            </div>
          )}

          {(can("resolve_job") || can("archive_job")) && (
            <div className="job-resolution-option job-resolution-option-row">
              {can("resolve_job") && (
                <button
                  className="btn-secondary"
                  disabled={busy === "resolve"}
                  onClick={handleResolve}
                >
                  {busy === "resolve" ? "…" : "Mark resolved"}
                </button>
              )}
              {can("archive_job") &&
                (!archiveConfirming ? (
                  <button className="btn-ghost-danger" onClick={() => setArchiveConfirming(true)}>
                    Archive
                  </button>
                ) : (
                  <button
                    className="btn-danger"
                    disabled={busy === "archive"}
                    onClick={handleArchive}
                  >
                    {busy === "archive" ? "…" : "Confirm archive"}
                  </button>
                ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
