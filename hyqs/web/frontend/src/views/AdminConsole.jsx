import { useEffect, useState } from "react";
import { streamJobs } from "../api.js";
import { useRole } from "../context.js";
import { canSeeAdminTab } from "../constants.js";
import { AdminCommandCenter } from "./AdminCommandCenter.jsx";
import { ProjectsView } from "./ProjectsView.jsx";
import { IntakeView } from "./IntakeView.jsx";
import { AdminProviders } from "./AdminProviders.jsx";
import { AdminAccess } from "./AdminAccess.jsx";
import { AdminUsageCost } from "./AdminUsageCost.jsx";
import { AdminVisitorAnalytics } from "./AdminVisitorAnalytics.jsx";

function Forbidden() {
  return <p className="hint">Not authorized.</p>;
}

export function AdminConsole({
  adminTab,
  subTab,
  adminUserId,
  onNavAdmin,
  onNavAdminUser,
  onNavWorkspace,
  projects,
  projectsLoaded,
  onReloadProjects,
}) {
  const [jobs, setJobs] = useState([]);
  const [note, setNote] = useState("");
  const { can } = useRole();

  useEffect(() => {
    const es = streamJobs(setJobs, () => {});
    return () => es.close();
  }, []);

  return (
    <main>
      {adminTab === "command-center" && (
        <AdminCommandCenter subTab={subTab} onNavTab={(t) => onNavAdmin("command-center", t)} />
      )}
      {adminTab === "projects" &&
        (canSeeAdminTab("projects", can) ? (
          <ProjectsView
            projects={projects}
            loading={!projectsLoaded}
            jobs={jobs}
            onOpen={(id) => onNavWorkspace(id, "dashboard")}
            onCreated={onReloadProjects}
            onNewProject={() => onNavAdmin("new-project")}
            can={can}
            note={note}
            setNote={setNote}
          />
        ) : (
          <Forbidden />
        ))}
      {adminTab === "new-project" &&
        (canSeeAdminTab("new-project", can) ? (
          <IntakeView onNavWorkspace={onNavWorkspace} onCancel={() => onNavAdmin("projects")} />
        ) : (
          <Forbidden />
        ))}
      {adminTab === "providers" &&
        (canSeeAdminTab("providers", can) ? <AdminProviders /> : <Forbidden />)}
      {adminTab === "access" && (
        <AdminAccess
          subTab={subTab}
          adminUserId={adminUserId}
          onNavTab={(t) => onNavAdmin("access", t)}
          onNavAdminUser={onNavAdminUser}
        />
      )}
      {adminTab === "usage-cost" &&
        (canSeeAdminTab("usage-cost", can) ? <AdminUsageCost /> : <Forbidden />)}
      {adminTab === "visitor-analytics" &&
        (canSeeAdminTab("visitor-analytics", can) ? <AdminVisitorAnalytics /> : <Forbidden />)}
    </main>
  );
}
