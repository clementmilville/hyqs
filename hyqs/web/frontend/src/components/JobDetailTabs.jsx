import { useEffect, useState } from "react";
import { getJobEvents } from "../api.js";
import { isActiveStatus } from "../constants.js";
import { JobOverviewTab } from "./JobOverviewTab.jsx";
import { JobLiveTab } from "./JobLiveTab.jsx";
import { JobTimeline } from "./JobTimeline.jsx";
import { JobDiffTab } from "./JobDiffTab.jsx";
import { JobHardwareTab } from "./JobHardwareTab.jsx";
import { JobDependenciesTab } from "./JobDependenciesTab.jsx";
import { JobCostTab } from "./JobCostTab.jsx";
import { JobResolutionPanel } from "./JobResolutionPanel.jsx";
import { FixBudgetPanel } from "./FixBudgetPanel.jsx";
import { useToast } from "./Toast.jsx";

export function defaultTab(job) {
  if (job.status === "failed") return "resolution";
  if (isActiveStatus(job.status)) return "live";
  return "overview";
}

const BASE_TABS = [
  { id: "overview", label: "OVERVIEW" },
  { id: "live", label: "LIVE" },
  { id: "stages", label: "STAGES" },
  { id: "diff", label: "DIFF" },
  { id: "hardware", label: "HARDWARE" },
  { id: "depends", label: "DEPENDS" },
  { id: "cost", label: "COST" },
];

export function JobDetailTabs({
  job,
  project,
  onOpenJob,
  inspectorMode = false,
  events: suppliedEvents,
}) {
  const [tab, setTab] = useState(() => (inspectorMode ? null : defaultTab(job)));
  const [loadedEvents, setLoadedEvents] = useState([]);
  const toast = useToast();
  const events = suppliedEvents ?? loadedEvents;

  const tabs =
    job.status === "failed" ? [{ id: "resolution", label: "RESOLUTION" }, ...BASE_TABS] : BASE_TABS;

  useEffect(() => {
    if (suppliedEvents !== undefined) return undefined;
    let live = true;
    const load = () =>
      getJobEvents(job.id)
        .then((e) => live && setLoadedEvents(e))
        .catch(() => {});
    load();
    const id = isActiveStatus(job.status) ? setInterval(load, 3000) : null;
    return () => {
      live = false;
      if (id) clearInterval(id);
    };
  }, [job.id, job.status, suppliedEvents]);

  return (
    <div className="job-tabs">
      <div className="job-tab-bar">
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            className={`job-tab-btn${tab === t.id ? " active" : ""}`}
            onClick={() => setTab(t.id)}
            aria-pressed={tab === t.id}
          >
            {t.label}
          </button>
        ))}
      </div>
      {tab === "resolution" && (
        <>
          <JobResolutionPanel job={job} />
          <FixBudgetPanel
            project={project}
            setNote={(msg) =>
              msg.startsWith("⚠️ ") ? toast.error(msg.slice("⚠️ ".length)) : toast.info(msg)
            }
            onChanged={() => {}}
          />
        </>
      )}
      {tab === "overview" && <JobOverviewTab job={job} events={events} />}
      {tab === "live" && <JobLiveTab job={job} />}
      {tab === "stages" && <JobTimeline job={job} events={events} autoExpandFailed={true} />}
      {tab === "diff" && <JobDiffTab jobId={job.id} />}
      {tab === "hardware" && <JobHardwareTab job={job} />}
      {tab === "depends" && <JobDependenciesTab job={job} onOpenJob={onOpenJob} />}
      {tab === "cost" && <JobCostTab job={job} />}
    </div>
  );
}
