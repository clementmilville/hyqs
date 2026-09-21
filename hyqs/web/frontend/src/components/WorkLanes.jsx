import { useEffect, useRef, useState } from "react";
import { ClipboardList } from "lucide-react";
import { GatedAction } from "../context.js";
import { useFleetData } from "../hooks/useFleetData.js";
import { DEFAULT_DESKTOP_COLUMNS } from "./jobTableShared.jsx";
import { WorkLaneSection } from "./WorkLaneSection.jsx";
import { WorkJobInspector } from "./WorkJobInspector.jsx";
import { FilterPresets } from "./FilterPresets.jsx";
import { WorkerCard } from "./WorkerCard.jsx";
import { EmptyState } from "./EmptyState.jsx";
import { WORK_VIEWS, WORK_VIEW_LABEL, WORK_GROUPINGS, validateWorkState } from "../constants.js";

const RECENTLY_FINISHED_LIMIT = 20;
const TERMINAL_STATUSES = new Set(["failed", "done", "cancelled"]);

function useWorkPresentation() {
  const classify = () => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") return "desktop";
    if (window.matchMedia("(max-width: 600px)").matches) return "mobile";
    if (window.matchMedia("(max-width: 900px)").matches) return "tablet";
    return "desktop";
  };
  const [mode, setMode] = useState(classify);
  useEffect(() => {
    if (typeof window.matchMedia !== "function") return undefined;
    const mobile = window.matchMedia("(max-width: 600px)");
    const tablet = window.matchMedia("(max-width: 900px)");
    const update = () => setMode(classify());
    mobile.addEventListener("change", update);
    tablet.addEventListener("change", update);
    return () => {
      mobile.removeEventListener("change", update);
      tablet.removeEventListener("change", update);
    };
  }, []);
  return mode;
}

function classifyJobs(jobs, supervisorEntries) {
  const supervisorById = new Map(supervisorEntries.map((entry) => [entry.job_id, entry]));
  const buckets = {
    failures: [],
    running: [],
    deploying: [],
    blocked: [],
    ready: [],
    history: [],
    archivedHistory: [],
  };

  for (const snapshotJob of jobs) {
    const job = { ...snapshotJob, ...supervisorById.get(snapshotJob.id) };
    if (snapshotJob.archived === true) buckets.archivedHistory.push(job);
    else if (job.status === "failed" && job.resolution !== "resolved") buckets.failures.push(job);
    else if (job.status === "running") buckets.running.push(job);
    else if (job.status === "deploying") buckets.deploying.push(job);
    else if (job.status === "pending" && job.waiting_on?.length > 0) buckets.blocked.push(job);
    else if (job.status === "pending" && job.scheduler_wait) buckets.blocked.push(job);
    else if (job.status === "pending") buckets.ready.push(job);
    else if (TERMINAL_STATUSES.has(job.status)) buckets.history.push(job);
  }

  const historyRank = (job) => {
    if (job.status === "failed") return job.resolution === "resolved" ? 1 : 0;
    if (job.status === "done") return 2;
    return 3;
  };
  const sortHistory = (a, b) =>
    historyRank(a) - historyRank(b) || String(b.updated_at).localeCompare(a.updated_at);
  buckets.history.sort(sortHistory);
  buckets.archivedHistory.sort(sortHistory);
  return buckets;
}

export function WorkLanes({
  project,
  epics,
  jobs,
  onNewJob,
  onOpenEpic,
  onOpenJob,
  workState,
  onWorkStateChange,
}) {
  const colStorageKey = `work_columns_${project.id}`;
  const presentation = useWorkPresentation();
  const inspectorOpenerRef = useRef(null);

  const [localWorkState, setLocalWorkState] = useState(() => validateWorkState(workState));
  const routedWorkState = validateWorkState(workState ?? localWorkState);
  const {
    view,
    epicId: selectedEpicId,
    search: searchQuery,
    grouping,
    selectedJobId,
  } = routedWorkState;
  const [hiddenCols, setHiddenCols] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(colStorageKey) || "[]"));
    } catch {
      return new Set();
    }
  });
  const [colDropOpen, setColDropOpen] = useState(false);
  const colDropRef = useRef(null);

  const { fleetData, supData, forbidden, error, retry, lastFleetAt: baseNow } = useFleetData();

  const epicsById = Object.fromEntries(epics.map((ep) => [ep.id, ep]));

  function changeWorkState(change) {
    const next = validateWorkState({ ...routedWorkState, ...change });
    setLocalWorkState(next);
    onWorkStateChange?.(next);
  }

  useEffect(() => {
    function handleClick(e) {
      if (colDropRef.current && !colDropRef.current.contains(e.target)) {
        setColDropOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  function toggleCol(key) {
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem(colStorageKey, JSON.stringify([...next]));
      return next;
    });
  }

  function loadPreset(state) {
    changeWorkState({
      search: state.searchQuery ?? searchQuery,
      epicId: state.selectedEpicId === undefined ? selectedEpicId : state.selectedEpicId,
    });
    if (state.hiddenCols) setHiddenCols(new Set(state.hiddenCols));
  }

  const { workers = [], pauses = [], merge_locks: mergeLocks = [] } = fleetData ?? {};
  const { needs_judgment: needsJudgment = [], dead_lettered: deadLettered = [] } = supData ?? {};

  const projectWorkers = workers.filter(
    (w) => w.project_id === project.id && w.status === "busy" && w.alive
  );
  const projectLocks = mergeLocks.filter((m) => m.repo_path === project.repo_path);
  const projectJudgment = needsJudgment.filter((j) => j.project_id === project.id);
  const projectDeadLettered = deadLettered.filter((j) => j.project_id === project.id);

  const lifecycle = classifyJobs(jobs, [...projectJudgment, ...projectDeadLettered]);

  function applyFilters(list) {
    return list.filter((j) => {
      if (selectedEpicId !== null && j.epic_id !== selectedEpicId) return false;
      if (
        searchQuery &&
        !(j.title || j.idea || "").toLowerCase().includes(searchQuery.toLowerCase())
      )
        return false;
      return true;
    });
  }

  const filteredFailures = applyFilters(lifecycle.failures);
  const filteredRunning = applyFilters(lifecycle.running);
  const filteredDeploying = applyFilters(lifecycle.deploying);
  const filteredBlocked = applyFilters(lifecycle.blocked);
  const filteredReady = applyFilters(lifecycle.ready);
  const filteredHistory = applyFilters(lifecycle.history).slice(0, RECENTLY_FINISHED_LIMIT);
  const filteredTerminalHistory = applyFilters([
    ...lifecycle.failures,
    ...lifecycle.history,
    ...lifecycle.archivedHistory,
  ])
    .sort((a, b) => {
      const rank = (job) => {
        if (job.status === "failed") return job.resolution === "resolved" ? 1 : 0;
        if (job.status === "done") return 2;
        return 3;
      };
      return rank(a) - rank(b) || String(b.updated_at).localeCompare(a.updated_at);
    })
    .slice(0, RECENTLY_FINISHED_LIMIT);

  const visibleCols = DEFAULT_DESKTOP_COLUMNS.filter((c) => !hiddenCols.has(c.key));
  const failureClassColumn = {
    key: "failure_class",
    label: "Class",
    render: (job) =>
      job.failure_class ? (
        <span className="badge" title={`Requeued ${job.supervisor_requeue_count ?? 0}×`}>
          {job.failure_class === "unknown" ? "Unclassified" : job.failure_class}
        </span>
      ) : null,
  };
  const waitingOnColumn = {
    key: "waiting_on",
    label: "Waiting On",
    render: (job) =>
      job.waiting_on?.length > 0 ? (
        <span className="waiting-badge">waiting on #{job.waiting_on.join(", #")}</span>
      ) : job.scheduler_wait ? (
        <span className="waiting-badge">{job.scheduler_wait.summary}</span>
      ) : null,
  };
  const nextActionColumn = {
    key: "next_action",
    label: "Next Action",
    render: (job) => {
      if (job.waiting_on?.length > 0) return `Complete #${job.waiting_on.join(", #")}`;
      if (job.scheduler_wait) return job.scheduler_wait.summary;
      return "Ready to start";
    },
  };

  const currentFilters = { searchQuery, selectedEpicId, hiddenCols: [...hiddenCols] };
  const liveLanesEmpty =
    filteredFailures.length === 0 &&
    filteredRunning.length === 0 &&
    filteredDeploying.length === 0 &&
    filteredBlocked.length === 0 &&
    filteredReady.length === 0;
  // Never render as four dead label rows: if every lane that signals active
  // work is empty but Recently Finished has data, show that instead of
  // making the user open a collapsed section to see anything at all.
  const autoExpandRecentlyFinished = liveLanesEmpty && filteredHistory.length > 0;
  const modeLanes =
    view === "queue"
      ? [
          { title: "Ready", jobs: filteredReady, extraColumns: [nextActionColumn] },
          {
            title: "Waiting",
            jobs: filteredBlocked,
            extraColumns: [waitingOnColumn, nextActionColumn],
          },
        ]
      : view === "history"
        ? [{ title: "History", jobs: filteredTerminalHistory }]
        : [
            {
              title: "Needs Attention",
              jobs: filteredFailures,
              extraColumns: [failureClassColumn],
            },
            { title: "Running Now", jobs: filteredRunning, workers: true },
            { title: "Deploying", jobs: filteredDeploying },
            { title: "Queued", jobs: filteredReady, extraColumns: [nextActionColumn] },
            {
              title: "Blocked",
              jobs: filteredBlocked,
              extraColumns: [waitingOnColumn, nextActionColumn],
            },
            { title: "Recently Finished", jobs: filteredHistory, recent: true },
          ];
  const modeJobs = modeLanes.flatMap((lane) => lane.jobs);
  const groupedByEpic = epics
    .map((epic) => ({ title: epic.name, jobs: modeJobs.filter((job) => job.epic_id === epic.id) }))
    .concat([{ title: "No epic", jobs: modeJobs.filter((job) => job.epic_id == null) }])
    .filter((group) => group.jobs.length > 0);
  const selectedJob = jobs.find((job) => job.id === selectedJobId);
  const selectJob = (jobId) => changeWorkState({ selectedJobId: jobId });
  const closeInspector = () => changeWorkState({ selectedJobId: null });

  const inspector = selectedJob && presentation !== "mobile" && (
    <WorkJobInspector
      job={selectedJob}
      project={project}
      mode={presentation}
      onOpenJob={onOpenJob}
      onClose={closeInspector}
      openerRef={inspectorOpenerRef}
      workerContext={projectWorkers.find((worker) => worker.job_id === selectedJob.id)}
      lineage={selectedJob.remediation_lineage || []}
    />
  );

  return (
    <div className="work-lanes-wrap">
      <div className="job-table-toolbar">
        <div className="job-table-toolbar-row2" aria-label="Work view">
          {WORK_VIEWS.map((workView) => (
            <button
              key={workView}
              className={`tap-target${view === workView ? " active" : ""}`}
              aria-pressed={view === workView}
              onClick={() => changeWorkState({ view: workView, selectedJobId: null })}
            >
              {WORK_VIEW_LABEL[workView]}
            </button>
          ))}
        </div>
        <label>
          Group by
          <select
            aria-label="Group by"
            value={grouping}
            onChange={(event) => changeWorkState({ grouping: event.target.value })}
          >
            {WORK_GROUPINGS.map((value) => (
              <option key={value} value={value}>
                {value === "status" ? "Status" : "Epic"}
              </option>
            ))}
          </select>
        </label>
        <label>
          Selected job
          <select
            aria-label="Selected job"
            value={selectedJobId ?? ""}
            onChange={(event) =>
              changeWorkState({
                selectedJobId: event.target.value ? Number(event.target.value) : null,
              })
            }
          >
            <option value="">None</option>
            {modeJobs.map((job) => (
              <option key={job.id} value={job.id}>
                #{job.id} {job.title || job.idea}
              </option>
            ))}
          </select>
        </label>
        {selectedJob && (
          <button
            ref={inspectorOpenerRef}
            className="btn-secondary tap-target"
            onClick={() => onOpenJob(selectedJob.id)}
          >
            Open #{selectedJob.id}: {selectedJob.title || selectedJob.idea}
          </button>
        )}
      </div>
      <div className="job-table-toolbar">
        <div className="filter-bar">
          <div className="filter-bar-chips">
            <button
              className={`tap-target${selectedEpicId === null ? " active" : ""}`}
              onClick={() => changeWorkState({ epicId: null })}
            >
              All
            </button>
            {epics.map((ep) => (
              <button
                key={ep.id}
                className={`tap-target${selectedEpicId === ep.id ? " active" : ""}`}
                onClick={() => changeWorkState({ epicId: ep.id })}
              >
                {ep.name}
              </button>
            ))}
          </div>
          <input
            value={searchQuery}
            onChange={(e) => changeWorkState({ search: e.target.value })}
            placeholder="Search…"
          />
        </div>

        <div className="job-table-toolbar-row2">
          <div className="job-table-toolbar-right">
            <div className="col-vis-wrap" ref={colDropRef}>
              <button
                className="btn-secondary col-vis-btn tap-target"
                onClick={() => setColDropOpen((v) => !v)}
              >
                View
              </button>
              {colDropOpen && (
                <div className="col-vis-dropdown">
                  <strong>Columns</strong>
                  {DEFAULT_DESKTOP_COLUMNS.map((c) => (
                    <label key={c.key} className="col-vis-item">
                      <input
                        type="checkbox"
                        checked={!hiddenCols.has(c.key)}
                        onChange={() => toggleCol(c.key)}
                      />
                      {c.label}
                    </label>
                  ))}
                  <strong>Saved views</strong>
                  <FilterPresets
                    projectId={project.id}
                    currentFilters={currentFilters}
                    onLoad={loadPreset}
                  />
                </div>
              )}
            </div>
            <GatedAction require="queue_job">
              <button className="tap-target" onClick={onNewJob}>
                + New Job
              </button>
            </GatedAction>
          </div>
        </div>
      </div>

      {forbidden && (
        <p className="hint">You don&apos;t have access to the runtime data for this project.</p>
      )}
      {error && (
        <div className="fleet-error">
          <p className="hint">Couldn&apos;t connect to the runtime feed.</p>
          <button className="tap-target" onClick={retry}>
            Retry
          </button>
        </div>
      )}

      {pauses.length > 0 && (
        <div className="fleet-banner warn">
          ⏸️ Paused:{" "}
          {pauses
            .map((p) => `${p.provider} (resumes ${new Date(p.until * 1000).toLocaleTimeString()})`)
            .join(", ")}
        </div>
      )}
      {projectLocks.length > 0 && (
        <div className="fleet-banner">
          🔒 Merging:{" "}
          {projectLocks.map((m) => `${m.repo_path.split("/").pop()} by ${m.owner}`).join(", ")}
        </div>
      )}

      <div className={`work-content work-content-${presentation}`}>
        {modeJobs.length === 0 ? (
          <EmptyState
            icon={ClipboardList}
            title="No jobs"
            hint={searchQuery ? "Try a different search." : `No jobs in ${WORK_VIEW_LABEL[view]}.`}
          />
        ) : grouping === "epic" ? (
          <div className="work-lanes">
            {groupedByEpic.map((group) => (
              <WorkLaneSection
                key={group.title}
                title={group.title}
                jobs={group.jobs}
                epicsById={epicsById}
                visibleCols={visibleCols}
                onOpenEpic={onOpenEpic}
                onOpenJob={onOpenJob}
                onSelectJob={selectJob}
                selectedJobId={selectedJobId}
                mobile={presentation === "mobile"}
                collapsible
              />
            ))}
          </div>
        ) : (
          <div className="work-lanes">
            {modeLanes.map((lane) => (
              <WorkLaneSection
                key={lane.title}
                title={lane.title}
                jobs={lane.jobs}
                epicsById={epicsById}
                visibleCols={
                  lane.recent || view === "history"
                    ? visibleCols.filter((column) => column.key !== "stage")
                    : visibleCols
                }
                extraColumns={lane.extraColumns}
                onOpenEpic={onOpenEpic}
                onOpenJob={onOpenJob}
                onSelectJob={selectJob}
                selectedJobId={selectedJobId}
                mobile={presentation === "mobile"}
                collapsible
                defaultCollapsed={lane.recent && !autoExpandRecentlyFinished}
                aboveTable={
                  lane.workers && projectWorkers.length > 0 ? (
                    <div className="work-lane-workers-strip">
                      {projectWorkers.map((worker) => (
                        <WorkerCard key={worker.id} w={worker} baseNow={baseNow} />
                      ))}
                    </div>
                  ) : null
                }
              />
            ))}
          </div>
        )}
        {presentation === "desktop" && inspector}
      </div>
      {presentation === "tablet" && selectedJob && (
        <div className="work-inspector-backdrop" onMouseDown={closeInspector}>
          <div onMouseDown={(event) => event.stopPropagation()}>{inspector}</div>
        </div>
      )}
    </div>
  );
}
