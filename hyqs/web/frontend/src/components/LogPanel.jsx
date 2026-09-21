import { useEffect, useRef, useState } from "react";
import { streamLogs } from "../api.js";
import { STAGE_LABEL } from "../constants.js";

export function LogPanel({ jobId, running }) {
  const [lines, setLines] = useState([]);
  const [streamError, setStreamError] = useState(false);
  const [retryGeneration, setRetryGeneration] = useState(0);
  const containerRef = useRef(null);

  useEffect(() => {
    setLines([]);
    setStreamError(false);
  }, [jobId]);

  useEffect(() => {
    if (!running) return;
    let active = true;
    const es = streamLogs(
      jobId,
      0,
      (rows) => {
        if (!active) return;
        setLines((prev) => [...prev, ...rows]);
      },
      () => {
        if (active) setStreamError(true);
      }
    );
    return () => {
      active = false;
      es.close();
    };
  }, [jobId, running, retryGeneration]);

  useEffect(() => {
    if (containerRef.current) {
      containerRef.current.scrollTop = containerRef.current.scrollHeight;
    }
  }, [lines]);

  if (lines.length === 0 && !running) return null;

  const retry = () => {
    setStreamError(false);
    setRetryGeneration((generation) => generation + 1);
  };

  // Group consecutive lines with the same (stage, attempt) pair.
  const groups = [];
  for (const row of lines) {
    const last = groups[groups.length - 1];
    const attempt = row.attempt ?? 0;
    if (last && last.stage === row.stage && last.attempt === attempt) {
      last.rows.push(row);
    } else {
      groups.push({ stage: row.stage, attempt, rows: [row] });
    }
  }

  // A group is "failed" when the same stage appears again in a later group.
  const seenLater = new Set();
  for (let i = groups.length - 1; i >= 0; i--) {
    groups[i].failed = seenLater.has(groups[i].stage);
    seenLater.add(groups[i].stage);
  }

  return (
    <div ref={containerRef} className="log-panel">
      {lines.length === 0 && !streamError && (
        <div className="log-stream-state" role="status">
          Waiting for log output…
        </div>
      )}
      {streamError && (
        <div className="log-stream-error" role="alert">
          <span>
            {lines.length === 0
              ? "Couldn’t connect to the log stream."
              : "The log stream was interrupted."}
          </span>
          <button type="button" className="log-stream-retry" onClick={retry}>
            Retry
          </button>
        </div>
      )}
      {groups.map((g, i) => (
        <div key={i}>
          <div className="log-attempt-header">
            {`── Attempt ${g.attempt + 1} · ${STAGE_LABEL[g.stage] ?? g.stage}`}
            {g.failed && <span className="log-attempt-failed"> ✗ failed</span>}
            {` ──`}
          </div>
          <pre className="log-attempt-body">{g.rows.map((r) => r.line).join("\n")}</pre>
        </div>
      ))}
    </div>
  );
}
