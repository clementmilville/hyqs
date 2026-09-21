import { isActiveStatus, STATUS_CLASS } from "../constants.js";
import { ago } from "../utils.js";
import { EmptyState } from "../components/EmptyState.jsx";
import { PageState } from "../components/PageState.jsx";

export function ProjectsView({
  projects,
  loading,
  forbidden = false,
  error = false,
  retry = () => {},
  jobs,
  onOpen,
  onNewProject,
  can,
  note,
}) {
  const showNew = onNewProject && (!can || can("create_project"));
  return (
    <div className="projects">
      {showNew && (
        <div className="projects-cta">
          <button className="btn-primary tap-target" onClick={onNewProject}>
            New Project
          </button>
        </div>
      )}
      {note && <p className="note">{note}</p>}
      <PageState
        forbidden={forbidden}
        error={error}
        loading={loading && projects.length === 0}
        retry={retry}
      >
        <div className="cards">
          {projects.length === 0 && (
            <EmptyState icon="📂" title="No projects yet" hint="Create one above to get started." />
          )}
          {projects.map((p) => {
            const active = jobs.filter(
              (j) => j.project_id === p.id && isActiveStatus(j.status)
            ).length;
            return (
              <button key={p.id} className="card" onClick={() => onOpen(p.id)}>
                <div className="card-top">
                  <span className="card-name">{p.name}</span>
                  <span className={`badge ${STATUS_CLASS[p.status]}`}>{p.status}</span>
                </div>
                <div className="card-sub">{p.repo_path}</div>
                <div className="card-meta">
                  {p.job_count} job{p.job_count === 1 ? "" : "s"}
                  {active > 0 && <span className="live"> · {active} active</span>}
                  <span> · {ago(p.last_activity)}</span>
                </div>
              </button>
            );
          })}
        </div>
      </PageState>
    </div>
  );
}
