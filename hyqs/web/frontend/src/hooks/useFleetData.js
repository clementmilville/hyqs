import { useEffect, useState } from "react";
import { streamWorkers, getWorkers, streamSupervisor, getSupervisor } from "../api.js";

// Owns the worker-fleet + supervisor EventSource subscriptions (and their
// initial fetches) as one unit, so every view reading fleet/supervisor data
// refreshes on the same tick instead of drifting apart on two independently
// timed connections. A stalled/forbidden connection surfaces as an explicit
// error state — callers must not swallow it into an eternal spinner.
export function useFleetData() {
  const [fleetData, setFleetData] = useState(null);
  const [supData, setSupData] = useState(null);
  const [error, setError] = useState(null); // null | "forbidden" | "error"
  const [lastFleetAt, setLastFleetAt] = useState(null);
  const [lastSupAt, setLastSupAt] = useState(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);

    function onStreamError() {
      if (cancelled) return;
      setError((prev) => prev || "error");
    }

    const esWorkers = streamWorkers((d) => {
      if (cancelled) return;
      setFleetData(d);
      setLastFleetAt(Date.now());
      setError(null);
    }, onStreamError);

    const esSup = streamSupervisor((d) => {
      if (cancelled) return;
      setSupData(d);
      setLastSupAt(Date.now());
      setError(null);
    }, onStreamError);

    Promise.all([getWorkers(), getSupervisor()])
      .then(([w, s]) => {
        if (cancelled) return;
        setFleetData(w);
        setSupData(s);
        const now = Date.now();
        setLastFleetAt(now);
        setLastSupAt(now);
      })
      .catch((e) => {
        if (cancelled) return;
        setError(e.message === "forbidden" ? "forbidden" : "error");
      });

    return () => {
      cancelled = true;
      esWorkers.close();
      esSup.close();
    };
  }, [attempt]);

  return {
    fleetData,
    supData,
    forbidden: error === "forbidden",
    error: error === "error",
    lastFleetAt,
    lastSupAt,
    retry: () => setAttempt((a) => a + 1),
  };
}
