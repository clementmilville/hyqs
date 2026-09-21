import { useContext } from "react";
import { ProjectContext } from "../context.js";

const TAB_LABEL = {
  dashboard: "Overview",
  work: "Work",
  plan: "Plan",
  history: "History",
  analytics: "Analytics",
  intake: "Intake",
  settings: "Settings",
};

export function PageHeader({ wsTab, onNavWorkspace, epicName }) {
  const { project } = useContext(ProjectContext);
  if (!project) return null;

  return (
    <div className="crumbs page-header">
      <button className="link" onClick={() => onNavWorkspace(null, "dashboard")}>
        Projects
      </button>
      <span className="crumb-sep">›</span>
      <button className="link" onClick={() => onNavWorkspace(project.id, "work")}>
        {project.name}
      </button>
      <span className="crumb-sep">›</span>
      {epicName ? (
        <>
          <button className="link" onClick={() => onNavWorkspace(project.id, "plan", "epics")}>
            Plan
          </button>
          <span className="crumb-sep">›</span>
          <span className="crumb-cur">{epicName}</span>
        </>
      ) : (
        <span className="crumb-cur">{TAB_LABEL[wsTab] ?? wsTab}</span>
      )}
    </div>
  );
}
