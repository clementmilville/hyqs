import { useState } from "react";
import { PLAN_TABS, PLAN_TAB_LABEL } from "../constants.js";
import { EpicsIndex } from "./EpicsIndex.jsx";
import { BacklogTab } from "./BacklogTab.jsx";
import { EpicDetail } from "./EpicDetail.jsx";
import { JobChatPanel } from "../components/JobChatPanel.jsx";

export function PlanTab({
  project,
  planTab,
  epics,
  jobs,
  epicId,
  epicTab,
  onNavEpic,
  onNavWorkspace,
  onChanged,
  note,
  setNote,
  showArchivedEpics,
  setShowArchivedEpics,
  ideate,
  architect,
  onOpenJob,
}) {
  const [chatOpen, setChatOpen] = useState(false);
  const [selectedEpicId, setSelectedEpicId] = useState(null);

  const openEpic = epicId != null ? (epics.find((e) => e.id === epicId) ?? null) : null;

  if (openEpic) {
    return (
      <div className="plan-tab">
        <EpicDetail
          epic={openEpic}
          project={project}
          jobs={jobs}
          epics={epics}
          tab={epicTab || "jobs"}
          onTabChange={(t) => onNavEpic(project.id, openEpic.id, t)}
          onNewJob={() => {
            setSelectedEpicId(openEpic.id);
            setChatOpen(true);
          }}
          ideate={ideate}
          architect={architect}
          onChanged={onChanged}
          setNote={setNote}
          onOpenJob={onOpenJob}
        />
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
              key={`${project.id}:${selectedEpicId ?? "none"}`}
              projectId={project.id}
              projectRepo={project.repo_path}
              epicId={selectedEpicId}
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

  return (
    <div className="plan-tab">
      <div className="job-tabs plan-sub-tabs">
        <div className="job-tab-bar">
          {PLAN_TABS.map((t) => (
            <button
              key={t}
              className={`job-tab-btn tap-target${planTab === t ? " active" : ""}`}
              onClick={() => onNavWorkspace(project.id, "plan", t)}
            >
              {PLAN_TAB_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      {note && <p className="note">{note}</p>}

      {planTab === "epics" ? (
        <EpicsIndex
          epics={epics}
          project={project}
          onChanged={onChanged}
          setNote={setNote}
          showArchivedEpics={showArchivedEpics}
          setShowArchivedEpics={setShowArchivedEpics}
          onOpenEpic={(epId) => onNavEpic(project.id, epId, "jobs")}
          onOpenIdeate={(ep) => {
            if (ideate.open(ep)) onNavEpic(project.id, ep.id, "ideate");
          }}
          onOpenArchitect={(ep) => {
            if (architect.open(ep)) onNavEpic(project.id, ep.id, "architect");
          }}
          ideateBusyEpicId={ideate.streaming ? (ideate.panel?.epicId ?? null) : null}
          architectBusyEpicId={architect.streaming ? (architect.panel?.epicId ?? null) : null}
        />
      ) : (
        <BacklogTab projectId={project.id} />
      )}
    </div>
  );
}
