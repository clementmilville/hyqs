import { getJobDependencyJobs, getJobDependents } from "../api.js";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "./PageState.jsx";
import { getStatusBadge, handleJobClick } from "./jobTableShared.jsx";

function DependencyRow({ depJob, onOpenJob }) {
  const badge = getStatusBadge(depJob);
  return (
    <button
      type="button"
      className="job-dep-row tap-target"
      onClick={() => handleJobClick(depJob, onOpenJob)}
    >
      <span className="job-dep-id">#{depJob.id}</span>
      <span className="job-dep-title">{depJob.title || depJob.idea}</span>
      <span className={badge.className} title={badge.title}>
        {badge.label}
      </span>
    </button>
  );
}

function DependencySection({ title, jobs, emptyHint, onOpenJob }) {
  return (
    <div className="job-dep-section">
      <h3 className="job-dep-section-title">{title}</h3>
      {jobs.length === 0 ? (
        <p className="hint">{emptyHint}</p>
      ) : (
        <div className="job-dep-list">
          {jobs.map((depJob) => (
            <DependencyRow key={depJob.id} depJob={depJob} onOpenJob={onOpenJob} />
          ))}
        </div>
      )}
    </div>
  );
}

export function JobDependenciesTab({ job, onOpenJob }) {
  const dependsOn = usePageData(() => getJobDependencyJobs(job.id), [job.id]);
  const dependents = usePageData(() => getJobDependents(job.id), [job.id]);

  return (
    <div className="job-dependencies-tab">
      <PageState
        forbidden={dependsOn.forbidden}
        error={dependsOn.error}
        loading={dependsOn.loading}
        retry={dependsOn.retry}
      >
        <DependencySection
          title="Depends on"
          jobs={dependsOn.data ?? []}
          emptyHint="No dependencies."
          onOpenJob={onOpenJob}
        />
      </PageState>
      <PageState
        forbidden={dependents.forbidden}
        error={dependents.error}
        loading={dependents.loading}
        retry={dependents.retry}
      >
        <DependencySection
          title="Dependents"
          jobs={dependents.data ?? []}
          emptyHint="No dependent jobs."
          onOpenJob={onOpenJob}
        />
      </PageState>
    </div>
  );
}
