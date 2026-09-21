import { useEffect, useState } from "react";
import { isActiveStatus, STATUS_CLASS } from "../constants.js";
import { ago } from "../utils.js";
import { GatedAction, useRole } from "../context.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";
import {
  listJobsFiltered,
  retryJob,
  listAgents,
  listEpics,
  getDeployStatus,
  triggerDeploy,
} from "../api.js";
import { JobChatPanel } from "../components/JobChatPanel.jsx";
import { useToast } from "../components/Toast.jsx";

export function WorkspaceDashboard({ project, jobs, note }) {
  const { can } = useRole();
  const toast = useToast();
  const [retryingId, setRetryingId] = useState(null);
  const [deploying, setDeploying] = useState(false);

  const running = jobs.filter(
    (job) => job.project_id === project?.id && isActiveStatus(job.status)
  );

  const {
    data: deployStatus,
    loading: deployLoading,
    forbidden: deployForbidden,
    error: deployError,
    retry: deployRetry,
  } = usePageData(
    () => (project ? getDeployStatus(project.id) : Promise.resolve(null)),
    [project?.id]
  );

  useEffect(() => {
    if (!project) return;
    const deployId = setInterval(deployRetry, 30000);
    return () => clearInterval(deployId);
  }, [project?.id, deployRetry]);

  const {
    data: pageData,
    loading,
    forbidden,
    error,
    retry,
  } = usePageData(async () => {
    if (!project) return { failedJobs: [], lastDone: null, epics: [], agents: [] };
    const [failedJobs, doneJobs, epics, agents] = await Promise.all([
      listJobsFiltered("failed", project.id),
      listJobsFiltered("done", project.id),
      listEpics(project.id),
      can("edit_project") ? listAgents(project.id) : Promise.resolve([]),
    ]);
    return { failedJobs, lastDone: doneJobs[0] ?? null, epics, agents };
  }, [project?.id, can]);

  const failedJobs = pageData?.failedJobs ?? [];
  const lastDone = pageData?.lastDone ?? null;
  const epics = pageData?.epics ?? [];
  const agents = pageData?.agents ?? [];

  async function handleDeploy() {
    if (deploying || !project) return;
    setDeploying(true);
    try {
      const result = await triggerDeploy(project.id);
      toast.success(`Deploy #${result.job_id} queued`);
      deployRetry();
    } catch (err) {
      toast.error(err.message || "Deploy failed");
    } finally {
      setDeploying(false);
    }
  }

  async function handleRetry(jobId) {
    if (retryingId) return;
    setRetryingId(jobId);
    try {
      await retryJob(jobId);
      retry();
    } catch (err) {
      toast.error(err.message || "Retry failed");
    } finally {
      setRetryingId(null);
    }
  }

  if (!project) return <p className="hint">Select a project to get started.</p>;

  const deployBadgeClass = deployStatus?.deployed_sha ? (deployStatus.is_stale ? "run" : "ok") : "";
  const deployBadgeTitle = deployStatus?.deployed_sha
    ? deployStatus.is_stale
      ? `Behind: deployed ${deployStatus.deployed_sha?.slice(0, 7)}, main is ${deployStatus.main_tip?.slice(0, 7)}`
      : `Live at ${deployStatus.deployed_sha?.slice(0, 7)}`
    : "No deploy recorded";
  const deployBadgeText = deployStatus?.deployed_sha
    ? deployStatus.is_stale
      ? `Behind (${deployStatus.deployed_sha?.slice(0, 7)})`
      : "Live"
    : "Never deployed";
  const deployButtonTitle = deployStatus?.deploy_in_flight
    ? `Deploy #${deployStatus.deploy_job_id} is in flight`
    : deploying
      ? "Deploying…"
      : "Deploy latest main now";
  const lastDoneText = lastDone
    ? `#${lastDone.id} ${lastDone.title || lastDone.idea} ${ago(lastDone.updated_at)}`
    : "";

  return (
    <div className="ws-dashboard">
      <div className="dash-card">
        <div className="dash-card-head">
          <span className="dash-card-title">{project.name}</span>
          <span className={`badge ${STATUS_CLASS[project.status]}`}>{project.status}</span>
          <GatedAction require="queue_job">
            <button
              className="tap-target"
              disabled={deployStatus?.deploy_in_flight || deploying}
              onClick={handleDeploy}
              title={deployButtonTitle}
            >
              {deploying ? "…" : "Deploy now"}
            </button>
          </GatedAction>
        </div>
        {project.description && <p className="dash-desc">{project.description}</p>}
        <div className="dash-stats" style={{ alignItems: "baseline" }}>
          <span>
            <span className="dash-stat-val" style={{ display: "inline" }}>
              {running.length}
            </span>{" "}
            <span className="dash-stat-label">running</span>
          </span>
          <PageState
            forbidden={deployForbidden}
            error={deployError}
            loading={deployLoading}
            retry={deployRetry}
          >
            <span className="dash-stat-label">
              {deployStatus?.deployed_at
                ? `Last deploy: ${ago(deployStatus.deployed_at)}`
                : "Never deployed"}
            </span>
            <span className={`badge ${deployBadgeClass}`} title={deployBadgeTitle}>
              {deployBadgeText}
            </span>
          </PageState>
        </div>
        {lastDone && (
          <div
            className="dash-last"
            style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}
            title={lastDoneText}
          >
            Last done: <b>#{lastDone.id}</b> {lastDone.title || lastDone.idea}{" "}
            <span className="dim">{ago(lastDone.updated_at)}</span>
          </div>
        )}
      </div>

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        <GatedAction require="queue_job">
          <>
            {failedJobs.length > 0 && (
              <div className="dash-card">
                <div className="dash-card-head">
                  <span className="dash-card-title">Needs attention</span>
                  <span className="badge bad">{failedJobs.length}</span>
                </div>
                <div className="jobs">
                  {failedJobs.slice(0, 5).map((j) => (
                    <div key={j.id} className="job bad">
                      <div className="jobhead">
                        <span className="jid">#{j.id}</span>
                        <span className="idea-inline">{j.title || j.idea}</span>
                        <span className="badge bad">{j.status}</span>
                        <GatedAction require="retry_job">
                          <button
                            className="retry-btn tap-target"
                            disabled={retryingId === j.id}
                            onClick={() => handleRetry(j.id)}
                          >
                            {retryingId === j.id ? "…" : "Retry"}
                          </button>
                        </GatedAction>
                      </div>
                      {j.error && <div className="err">{j.error}</div>}
                    </div>
                  ))}
                </div>
              </div>
            )}
            <div className="dash-card">
              <div className="dash-card-head">
                <span className="dash-card-title">Queue a job</span>
              </div>
              <JobChatPanel
                key={project.id}
                projectId={project.id}
                projectRepo={project.repo_path}
                epicId={null}
                epics={epics}
                onJobCreated={() => {}}
              />
            </div>
          </>
        </GatedAction>

        <GatedAction require="edit_project">
          <div className="dash-card">
            <div className="dash-card-head">
              <span className="dash-card-title">Config health</span>
            </div>
            <div className="dash-health">
              <div className={`health-chip ${agents.length > 0 ? "ok" : "bad"}`}>
                {agents.length > 0
                  ? `✓ ${agents.length} agent${agents.length === 1 ? "" : "s"} configured`
                  : "✗ No agents configured"}
              </div>
              <div className="health-chip">
                Fix budget: {project.max_fix_attempts ?? "default (3)"}
              </div>
              <div className="health-chip dim">Lint: auto-detected from project files</div>
            </div>
          </div>
        </GatedAction>
      </PageState>

      {note && <p className="note">{note}</p>}
    </div>
  );
}
