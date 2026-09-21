import { LogPanel } from "./LogPanel.jsx";
import { isActiveStatus } from "../constants.js";
import { getStatusBadge } from "./jobTableShared.jsx";

export function JobLiveTab({ job }) {
  if (isActiveStatus(job.status)) {
    return <LogPanel jobId={job.id} running={true} />;
  }
  const statusBadge = getStatusBadge(job);
  return (
    <div className="job-live-idle">
      <span>Job is not currently running</span>
      <span className={statusBadge.className} title={statusBadge.title}>
        {statusBadge.label}
      </span>
    </div>
  );
}
