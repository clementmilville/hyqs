import { useEffect, useState } from "react";
import { getJobDiff } from "../api.js";

export function JobDiffTab({ jobId }) {
  const [diff, setDiff] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    let live = true;
    setLoading(true);
    setError(null);
    setDiff(null);
    getJobDiff(jobId)
      .then((d) => {
        if (live) {
          setDiff(d);
          setLoading(false);
        }
      })
      .catch((err) => {
        if (live) {
          setError(err.message || "Failed to load diff");
          setLoading(false);
        }
      });
    return () => {
      live = false;
    };
  }, [jobId]);

  if (loading) return <p className="hint">Loading diff…</p>;
  if (error)
    return (
      <p className="hint" style={{ color: "var(--bad)" }}>
        {error}
      </p>
    );
  if (!diff.branch) return <p className="hint">No diff yet — this job hasn't branched.</p>;

  return (
    <div className="difful">
      <div className="diffhead">
        <b>
          diff {diff.base}...{diff.branch}
        </b>
      </div>
      <pre className="diff">
        {diff.diff || "(empty)"}
        {diff.truncated ? "\n… (truncated)" : ""}
      </pre>
    </div>
  );
}
