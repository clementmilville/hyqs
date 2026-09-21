import { HISTORY_TABS, HISTORY_TAB_LABEL } from "../constants.js";
import { ChangelogTab } from "./ChangelogTab.jsx";
import { DecisionsTab } from "./DecisionsTab.jsx";
import { JobTable } from "../components/JobTable.jsx";

export function HistoryTab({ project, subTab, onNavWorkspace, epics = [], jobs = [], onOpenJob }) {
  return (
    <div className="history-tab">
      <div className="job-tabs plan-sub-tabs">
        <div className="job-tab-bar">
          {HISTORY_TABS.map((t) => (
            <button
              key={t}
              className={`job-tab-btn tap-target${subTab === t ? " active" : ""}`}
              onClick={() => onNavWorkspace(project.id, "history", t)}
            >
              {HISTORY_TAB_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      {subTab === "decisions" ? (
        <DecisionsTab projectId={project.id} />
      ) : subTab === "archived" ? (
        <JobTable
          jobs={jobs}
          epics={epics}
          project={project}
          selectedEpicId={null}
          initialStatusFilter="archived"
          onNewJob={() => onNavWorkspace(project.id, "work")}
          onChanged={() => {}}
          onOpenJob={onOpenJob}
        />
      ) : (
        <ChangelogTab projectId={project.id} />
      )}
    </div>
  );
}
