import { useState } from "react";
import { PerfFilterBar } from "../components/PerfFilterBar.jsx";
import { PerfOverview } from "./perf/PerfOverview.jsx";
import { PerfJobDetail } from "./perf/PerfJobDetail.jsx";

const EMPTY_FILTERS = { from: "", to: "", stage: "", epic_id: null, status: "", provider: "" };

export function PerformanceTab({ projectId }) {
  const [filters, setFilters] = useState(EMPTY_FILTERS);
  const [selectedJob, setSelectedJob] = useState(null);

  if (selectedJob !== null) {
    return (
      <div className="performance-tab">
        <PerfJobDetail job={selectedJob} onBack={() => setSelectedJob(null)} />
      </div>
    );
  }

  return (
    <div className="performance-tab">
      <p className="hint">Pipeline metrics exclude operational auto-deploy jobs.</p>
      <PerfFilterBar projectId={projectId} filters={filters} onChange={setFilters} />

      <div style={{ marginTop: "var(--space-6)" }}>
        <PerfOverview projectId={projectId} filters={filters} onSelectJob={setSelectedJob} />
      </div>
    </div>
  );
}
