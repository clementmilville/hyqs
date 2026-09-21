import { useEffect, useState } from "react";
import { getJobEvents } from "../../api.js";
import { JobTimeline } from "../../components/JobTimeline.jsx";
import { PageState } from "../../components/PageState.jsx";

const STATUS_CLASS = { done: "ok", failed: "bad", running: "run", cancelled: "bad" };

export function PerfJobDetail({ job, onBack }) {
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null); // null | "forbidden" | "error"
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setStatus(null);
    const load = () =>
      getJobEvents(job.job_id)
        .then((e) => {
          if (!live) return;
          setEvents(e);
          setStatus(null);
          setLoading(false);
        })
        .catch((e) => {
          if (!live) return;
          setStatus(e.message === "forbidden" ? "forbidden" : "error");
          setLoading(false);
        });
    load();
    const active = job.status === "pending" || job.status === "running";
    const id = active ? setInterval(load, 3000) : null;
    return () => {
      live = false;
      if (id) clearInterval(id);
    };
  }, [job.job_id, job.status, attempt]);

  // JobTimeline expects job.id; normalise from the perf-row's job_id
  const tlJob = { ...job, id: job.job_id };
  const hasEvents = events.length > 0;

  return (
    <div>
      <button className="perf-back-btn tap-target" onClick={onBack}>
        ← Back
      </button>
      <div className="perf-job-detail-header">
        <span className="perf-job-detail-id">#{job.job_id}</span>
        <span className="perf-job-detail-idea">{job.title || job.idea}</span>
        <span className={`badge ${STATUS_CLASS[job.status] || ""}`}>{job.status}</span>
      </div>
      <PageState
        forbidden={status === "forbidden" && !hasEvents}
        error={status === "error" && !hasEvents}
        loading={loading && !hasEvents}
        retry={() => setAttempt((a) => a + 1)}
      >
        <JobTimeline job={tlJob} events={events} autoExpandFailed={true} />
      </PageState>
    </div>
  );
}
