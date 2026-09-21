import { useEffect, useState, useCallback } from "react";

// A single-fetch counterpart to useFleetData.js: owns one async fetch call,
// never swallows its error into `.catch(() => {})`, and distinguishes a
// `"forbidden"` failure (`e.message === "forbidden"`) from any other
// connection error so the caller can render the CONVENTIONS.md §9 three-state
// trio (forbidden / error / loading) via PageState.jsx. `fetchFn` is called
// again whenever `deps` changes or `retry()` is invoked.
export function usePageData(fetchFn, deps = []) {
  const [data, setData] = useState(null);
  const [status, setStatus] = useState(null); // null | "forbidden" | "error"
  const [loading, setLoading] = useState(true);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setStatus(null);
    fetchFn()
      .then((d) => {
        if (cancelled) return;
        setData(d);
        setLoading(false);
      })
      .catch((e) => {
        if (cancelled) return;
        setStatus(e.message === "forbidden" ? "forbidden" : "error");
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [attempt, ...deps]);

  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  return {
    data,
    loading,
    forbidden: status === "forbidden",
    error: status === "error",
    retry,
  };
}
