import { useEffect, useState } from "react";
import { listEpics } from "../api.js";

const STAGES = ["", "plan", "build", "fix", "review", "merge"];
const STATUSES = ["", "pending", "running", "done", "failed", "cancelled"];
const PROVIDERS = ["", "claude", "codex", "openai"];

const EMPTY = { from: "", to: "", stage: "", epic_id: null, status: "", provider: "" };

function useIsMobile() {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia("(max-width: 767px)").matches
  );
  useEffect(() => {
    const mq = window.matchMedia("(max-width: 767px)");
    const handler = (e) => setIsMobile(e.matches);
    mq.addEventListener("change", handler);
    return () => mq.removeEventListener("change", handler);
  }, []);
  return isMobile;
}

export function PerfFilterBar({ projectId, filters, onChange }) {
  const [epics, setEpics] = useState([]);
  const isMobile = useIsMobile();

  useEffect(() => {
    if (!projectId) return;
    listEpics(projectId)
      .then(setEpics)
      .catch(() => {});
  }, [projectId]);

  function update(patch) {
    onChange({ ...filters, ...patch });
  }

  function reset() {
    onChange({ ...EMPTY });
  }

  const hasFilter =
    filters.from ||
    filters.to ||
    filters.stage ||
    filters.epic_id != null ||
    filters.status ||
    filters.provider;

  const fields = (
    <>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">From</span>
        <input
          type="date"
          value={filters.from || ""}
          onChange={(e) => update({ from: e.target.value || "" })}
        />
      </label>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">To</span>
        <input
          type="date"
          value={filters.to || ""}
          onChange={(e) => update({ to: e.target.value || "" })}
        />
      </label>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">Epic</span>
        <select
          value={filters.epic_id ?? ""}
          onChange={(e) => update({ epic_id: e.target.value ? Number(e.target.value) : null })}
          style={{ maxWidth: "100%" }}
        >
          <option value="">All epics</option>
          {epics.map((ep) => (
            <option key={ep.id} value={ep.id}>
              {ep.name}
            </option>
          ))}
        </select>
      </label>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">Stage</span>
        <select
          value={filters.stage || ""}
          onChange={(e) => update({ stage: e.target.value || "" })}
          style={{ maxWidth: "100%" }}
        >
          {STAGES.map((s) => (
            <option key={s} value={s}>
              {s || "All stages"}
            </option>
          ))}
        </select>
      </label>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">Status</span>
        <select
          value={filters.status || ""}
          onChange={(e) => update({ status: e.target.value || "" })}
          style={{ maxWidth: "100%" }}
        >
          {STATUSES.map((s) => (
            <option key={s} value={s}>
              {s || "All statuses"}
            </option>
          ))}
        </select>
      </label>
      <label className="perf-filter-field tap-target">
        <span className="perf-filter-label">Provider</span>
        <select
          value={filters.provider || ""}
          onChange={(e) => update({ provider: e.target.value || "" })}
          style={{ maxWidth: "100%" }}
        >
          {PROVIDERS.map((p) => (
            <option key={p} value={p}>
              {p || "All providers"}
            </option>
          ))}
        </select>
      </label>
      {hasFilter && (
        <button
          className="btn-secondary tap-target"
          onClick={reset}
          style={{ alignSelf: "flex-end" }}
        >
          Reset
        </button>
      )}
    </>
  );

  if (isMobile) {
    return (
      <details>
        <summary className="tap-target">Filters{hasFilter ? " •" : ""}</summary>
        <div className="perf-filter-bar">{fields}</div>
      </details>
    );
  }

  return <div className="perf-filter-bar">{fields}</div>;
}
