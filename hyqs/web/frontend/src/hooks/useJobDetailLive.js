import { useEffect, useState } from "react";
import { getJob, getJobEvents, streamJobDetail } from "../api.js";

export function useJobDetailLive(jobId) {
  const [job, setJob] = useState(null);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(true);
  const [status, setStatus] = useState(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStatus(null);

    function handleError(error) {
      if (cancelled) return;
      setStatus(error?.message === "forbidden" ? "forbidden" : "error");
      setLoading(false);
    }

    const eventSource = streamJobDetail(
      jobId,
      (snapshot) => {
        if (cancelled) return;
        setJob(snapshot.job);
        setEvents(snapshot.events);
        setStatus(null);
      },
      handleError
    );

    Promise.all([getJob(jobId), getJobEvents(jobId)])
      .then(([nextJob, nextEvents]) => {
        if (cancelled) return;
        setJob(nextJob);
        setEvents(nextEvents);
        setStatus(null);
        setLoading(false);
      })
      .catch(handleError);

    return () => {
      cancelled = true;
      eventSource.close();
    };
  }, [jobId, attempt]);

  return {
    job,
    events,
    loading,
    forbidden: status === "forbidden",
    error: status === "error",
    retry: () => setAttempt((current) => current + 1),
  };
}
