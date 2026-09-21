import { useEffect, useState } from "react";
import { ProjectContext, useRole } from "../context.js";
import {
  streamJobs,
  listEpics,
  streamSuggest,
  streamArchitectPlan,
  createJob,
  createBatchJobs,
} from "../api.js";
import { useAIStream } from "../hooks/useAIStream.js";
import { WorkspaceDashboard } from "./WorkspaceDashboard.jsx";
import { ProjectDetail } from "./ProjectDetail.jsx";
import { ProjectsView } from "./ProjectsView.jsx";
import { WorkspaceSettings } from "../components/WorkspaceSettings.jsx";
import { PageHeader } from "../components/PageHeader.jsx";
import { PlanTab } from "./PlanTab.jsx";
import { HistoryTab } from "./HistoryTab.jsx";
import { AnalyticsTab } from "./AnalyticsTab.jsx";
import { JobDetailPage } from "./JobDetailPage.jsx";

export function WorkspaceShell({
  projectId,
  wsTab,
  epicId,
  epicTab,
  subTab,
  jobId,
  workState,
  projects,
  projectsLoaded,
  onNavWorkspace,
  onNavEpic,
  onNavAdmin,
  onNavJob,
  onWorkStateChange,
  onBackJob,
  onReloadProjects,
}) {
  const { can } = useRole();
  const [jobs, setJobs] = useState([]);
  const [note, setNote] = useState("");
  const [showArchivedEpics, setShowArchivedEpics] = useState(false);
  const [epics, setEpics] = useState([]);

  // Ideate/architect stream state lives here (not in EpicsIndex/EpicDetail, which
  // unmount on tab/route changes) so an in-progress stream survives navigation.
  const [suggestPanel, setSuggestPanel] = useState(null);
  const ideateStream = useAIStream();
  const [architectPanel, setArchitectPanel] = useState(null);
  const architectStream = useAIStream();
  const [ideateInFlight, setIdeateInFlight] = useState(false);
  const [architectInFlight, setArchitectInFlight] = useState(false);

  const currentProject = projects.find((p) => p.id === projectId) ?? null;
  const currentProjectId = currentProject?.id ?? null;

  useEffect(() => {
    if (!ideateStream.result?.suggestions) return;
    setSuggestPanel((cur) =>
      cur ? { ...cur, suggestions: ideateStream.result.suggestions, checked: {}, done: true } : null
    );
  }, [ideateStream.result]);

  useEffect(() => {
    if (!ideateStream.error) return;
    setNote("⚠️ " + ideateStream.error.message);
    setSuggestPanel(null);
  }, [ideateStream.error]);

  useEffect(() => {
    if (!architectStream.result) return;
    setArchitectPanel((cur) =>
      cur
        ? {
            ...cur,
            jobs: Array.isArray(architectStream.result.jobs) ? architectStream.result.jobs : [],
            summary: architectStream.result.summary || "",
            checked: {},
            done: true,
          }
        : null
    );
  }, [architectStream.result]);

  useEffect(() => {
    if (!architectStream.error) return;
    setNote("⚠️ " + architectStream.error.message);
    setArchitectPanel(null);
  }, [architectStream.error]);

  function openSuggest(ep) {
    if (ideateStream.streaming) return false;
    setSuggestPanel({ epicId: ep.id, suggestions: [], checked: {}, done: false });
    ideateStream.start(streamSuggest, ep.id);
    return true;
  }

  function stopSuggest() {
    ideateStream.stop();
    setSuggestPanel(null);
  }

  async function createSelectedSuggestions(ep) {
    if (ideateInFlight) return;
    setIdeateInFlight(true);
    const panel = suggestPanel;
    const toCreate = panel.suggestions.filter((_, i) => panel.checked[i]);
    try {
      for (const s of toCreate) {
        try {
          await createJob(`${s.title}: ${s.description}`, currentProject?.repo_path, ep.id);
        } catch (err) {
          setNote("⚠️ " + err.message);
          return;
        }
      }
      setSuggestPanel(null);
      onChanged();
      setNote(`${toCreate.length} job${toCreate.length === 1 ? "" : "s"} queued.`);
    } finally {
      setIdeateInFlight(false);
    }
  }

  function openArchitect(ep) {
    if (architectStream.streaming) return false;
    const goal = window.prompt("What should this epic accomplish? Describe the goal:");
    if (!goal || !goal.trim()) return false;
    setArchitectPanel({ epicId: ep.id, jobs: [], checked: {}, done: false, summary: "" });
    architectStream.start(streamArchitectPlan, ep.id, goal.trim());
    return true;
  }

  function stopArchitect() {
    architectStream.stop();
    setArchitectPanel(null);
  }

  async function createSelectedArchitectJobs(ep) {
    if (architectInFlight) return;
    setArchitectInFlight(true);
    const panel = architectPanel;
    const selected = panel.jobs.map((j, i) => ({ ...j, _i: i })).filter((j) => panel.checked[j._i]);
    if (selected.length === 0) {
      setArchitectInFlight(false);
      return;
    }
    const indexMap = new Map(selected.map((j, newIdx) => [j._i, newIdx]));
    const jobsToCreate = selected.map((j) => ({
      title: j.title,
      description: j.idea,
      epic_id: ep.id,
      target_files: j.target_files || [],
      depends_on: (j.depends_on || []).filter((d) => indexMap.has(d)).map((d) => indexMap.get(d)),
      scope: j.scope || null,
      priority: j.priority || 0,
    }));
    try {
      await createBatchJobs(currentProject?.repo_path, jobsToCreate);
      setArchitectPanel(null);
      onChanged();
      setNote(`${jobsToCreate.length} job${jobsToCreate.length === 1 ? "" : "s"} queued.`);
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setArchitectInFlight(false);
    }
  }

  const ideate = {
    panel: suggestPanel,
    streaming: ideateStream.streaming,
    text: ideateStream.text,
    toolEvents: ideateStream.toolEvents,
    open: openSuggest,
    stop: stopSuggest,
    toggleChecked: (i) =>
      setSuggestPanel((p) => (p ? { ...p, checked: { ...p.checked, [i]: !p.checked[i] } } : p)),
    createSelected: createSelectedSuggestions,
    dismiss: () => setSuggestPanel(null),
  };

  const architect = {
    panel: architectPanel,
    streaming: architectStream.streaming,
    text: architectStream.text,
    toolEvents: architectStream.toolEvents,
    open: openArchitect,
    stop: stopArchitect,
    toggleChecked: (i) =>
      setArchitectPanel((p) => (p ? { ...p, checked: { ...p.checked, [i]: !p.checked[i] } } : p)),
    createSelected: createSelectedArchitectJobs,
    dismiss: () => setArchitectPanel(null),
  };

  useEffect(() => {
    const es = streamJobs(setJobs, () => {});
    return () => es.close();
  }, []);

  const reloadEpics = (pid, includeArchived = false) =>
    listEpics(pid, includeArchived)
      .then(setEpics)
      .catch(() => {});

  useEffect(() => {
    if (!currentProjectId) return;
    reloadEpics(currentProjectId, showArchivedEpics);
    const id = setInterval(() => reloadEpics(currentProjectId, showArchivedEpics), 5000);
    return () => clearInterval(id);
  }, [currentProjectId, showArchivedEpics]);

  useEffect(() => {
    if (currentProjectId) localStorage.setItem("hyqs_last_project", String(currentProjectId));
  }, [currentProjectId]);

  const projJobs = jobs.filter((j) => j.project_id === currentProjectId);
  const openJob = (jid) => onNavJob(currentProjectId, jid);
  const onChanged = () => {
    onReloadProjects?.();
    if (currentProjectId) reloadEpics(currentProjectId, showArchivedEpics);
  };

  const noProjectTabs = [];
  const showProjectsView = !currentProject && !noProjectTabs.includes(wsTab);
  const epicName =
    wsTab === "plan" && epicId != null ? (epics.find((e) => e.id === epicId)?.name ?? null) : null;

  if (jobId != null && currentProject) {
    return (
      <ProjectContext.Provider value={{ project: currentProject }}>
        <main>
          <JobDetailPage
            jobId={jobId}
            project={currentProject}
            epics={epics}
            onBack={() => (onBackJob ? onBackJob() : onNavWorkspace(currentProjectId, "work"))}
            onOpenJob={openJob}
          />
        </main>
      </ProjectContext.Provider>
    );
  }

  return (
    <ProjectContext.Provider value={{ project: currentProject }}>
      <main>
        {currentProject && (
          <PageHeader wsTab={wsTab} onNavWorkspace={onNavWorkspace} epicName={epicName} />
        )}

        {wsTab === "dashboard" && !showProjectsView && (
          <WorkspaceDashboard
            project={currentProject}
            jobs={projJobs}
            note={note}
            setNote={setNote}
            onChanged={onChanged}
          />
        )}
        {wsTab === "work" && currentProject && (
          <ProjectDetail
            project={currentProject}
            epics={epics}
            jobs={projJobs}
            onChanged={onChanged}
            note={note}
            onNavEpic={onNavEpic}
            onOpenJob={openJob}
            workState={workState}
            onWorkStateChange={onWorkStateChange}
          />
        )}
        {wsTab === "plan" && currentProject && (
          <PlanTab
            project={currentProject}
            planTab={subTab}
            epics={epics}
            jobs={projJobs}
            epicId={epicId}
            epicTab={epicTab}
            onNavEpic={onNavEpic}
            onNavWorkspace={onNavWorkspace}
            onChanged={onChanged}
            note={note}
            setNote={setNote}
            showArchivedEpics={showArchivedEpics}
            setShowArchivedEpics={setShowArchivedEpics}
            ideate={ideate}
            architect={architect}
            onOpenJob={openJob}
          />
        )}
        {wsTab === "history" && currentProject && (
          <HistoryTab
            project={currentProject}
            subTab={subTab}
            onNavWorkspace={onNavWorkspace}
            epics={epics}
            jobs={projJobs}
            onOpenJob={openJob}
          />
        )}
        {wsTab === "analytics" && currentProject && <AnalyticsTab project={currentProject} />}
        {wsTab === "settings" && currentProject && (
          <WorkspaceSettings
            project={currentProject}
            setNote={setNote}
            onChanged={onChanged}
            onDeleted={() => {
              onReloadProjects?.();
              onNavWorkspace(null, "dashboard");
            }}
          />
        )}

        {showProjectsView && (
          <ProjectsView
            projects={projects}
            loading={!projectsLoaded}
            jobs={jobs}
            onOpen={(id) => onNavWorkspace(id, "work")}
            onCreated={onReloadProjects}
            onNewProject={() => onNavAdmin("new-project")}
            can={can}
            note={note}
            setNote={setNote}
          />
        )}
      </main>
    </ProjectContext.Provider>
  );
}
