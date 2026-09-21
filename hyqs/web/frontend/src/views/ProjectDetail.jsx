import { useEffect, useMemo, useState } from "react";
import { WorkLanes } from "../components/WorkLanes.jsx";
import { JobChatPanel } from "../components/JobChatPanel.jsx";
import { PageState } from "../components/PageState.jsx";
import { listJobsFiltered } from "../api.js";

const TERMINAL_STATUSES = new Set(["failed", "done", "cancelled"]);

export function ProjectDetail({
  project,
  epics,
  jobs,
  onChanged,
  note,
  onNavEpic,
  onOpenJob,
  workState,
  onWorkStateChange,
}) {
  const hasSessionParam = (() => {
    try {
      return !!new URLSearchParams(window.location.search).get("session");
    } catch {
      return false;
    }
  })();
  const [chatOpen, setChatOpen] = useState(hasSessionParam);
  const [refreshedSnapshot, setRefreshedSnapshot] = useState({ projectId: null, jobs: [] });
  const [refreshError, setRefreshError] = useState({ projectId: null, error: null });

  const projJobs = jobs.filter((j) => j.project_id === project.id);
  useEffect(() => {
    let live = true;
    const load = () =>
      listJobsFiltered("all", project.id)
        .then((nextJobs) => {
          if (live) {
            setRefreshedSnapshot({ projectId: project.id, jobs: nextJobs });
            setRefreshError({ projectId: project.id, error: null });
          }
        })
        .catch((error) => {
          if (live) setRefreshError({ projectId: project.id, error });
        });
    load();
    const intervalId = setInterval(load, 5000);
    return () => {
      live = false;
      clearInterval(intervalId);
    };
  }, [project.id]);

  const lifecycleJobs = useMemo(() => {
    const refreshedJobs = refreshedSnapshot.projectId === project.id ? refreshedSnapshot.jobs : [];
    const byId = new Map(refreshedJobs.map((job) => [job.id, job]));
    for (const liveJob of projJobs) {
      const refreshedJob = byId.get(liveJob.id);
      if (!refreshedJob || !TERMINAL_STATUSES.has(refreshedJob.status)) {
        byId.set(liveJob.id, liveJob);
      }
    }
    return [...byId.values()];
  }, [projJobs, project.id, refreshedSnapshot]);

  const currentRefreshError = refreshError.projectId === project.id ? refreshError.error : null;
  const initialRefreshLoading =
    refreshedSnapshot.projectId !== project.id && currentRefreshError === null;

  return (
    <div className="work-pane">
      <div className="job-pane">
        {note && <p className="note">{note}</p>}
        {currentRefreshError && (
          <p className="hint">Couldn&apos;t refresh completed work; live work is still shown.</p>
        )}
        <PageState loading={initialRefreshLoading}>
          <WorkLanes
            jobs={lifecycleJobs}
            epics={epics}
            project={project}
            onNewJob={() => setChatOpen(true)}
            onChanged={onChanged}
            onOpenEpic={(epId) => onNavEpic(project.id, epId, "jobs")}
            onOpenJob={onOpenJob}
            workState={workState}
            onWorkStateChange={onWorkStateChange}
          />
        </PageState>
      </div>
      {chatOpen && (
        <div className="job-chat-drawer">
          <button
            className="job-chat-drawer-close tap-target"
            onClick={() => setChatOpen(false)}
            aria-label="Close chat"
          >
            ✕
          </button>
          <JobChatPanel
            key={project.id}
            projectId={project.id}
            projectRepo={project.repo_path}
            epicId={null}
            epics={epics}
            onJobCreated={() => {
              setChatOpen(false);
              onChanged();
            }}
          />
        </div>
      )}
    </div>
  );
}
