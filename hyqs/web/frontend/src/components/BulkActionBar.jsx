import { useState } from "react";
import { GatedAction } from "../context.js";

export function BulkActionBar({ selectedIds, epics, onArchive, onMoveToEpic, onClearSelection }) {
  const [archiving, setArchiving] = useState(false);
  const [moving, setMoving] = useState(false);
  const n = selectedIds.length;

  async function handleArchive() {
    if (archiving) return;
    setArchiving(true);
    try {
      await onArchive(selectedIds);
    } finally {
      setArchiving(false);
    }
  }

  async function handleMoveSelect(e) {
    const val = e.target.value;
    if (val === "") return;
    e.target.value = "";
    if (moving) return;
    setMoving(true);
    try {
      await onMoveToEpic(selectedIds, val === "__unassigned__" ? null : +val);
    } finally {
      setMoving(false);
    }
  }

  return (
    <div className="bulk-action-bar">
      <span className="bulk-count">{n} selected</span>
      <GatedAction require="archive_job" fallback={null}>
        <button
          className="btn-secondary tap-target"
          disabled={archiving || moving}
          onClick={handleArchive}
        >
          {archiving ? "Archiving…" : `Archive ${n} job${n === 1 ? "" : "s"}`}
        </button>
      </GatedAction>
      <GatedAction require="edit_job_deps" fallback={null}>
        <select
          className="bulk-epic-select"
          aria-label="Move selected jobs to epic"
          disabled={moving || archiving}
          value=""
          onChange={handleMoveSelect}
        >
          <option value="">Move to epic…</option>
          <option value="__unassigned__">— Unassigned —</option>
          {epics
            .filter((ep) => !ep.archived)
            .map((ep) => (
              <option key={ep.id} value={ep.id}>
                {ep.name}
              </option>
            ))}
        </select>
      </GatedAction>
      <button className="link tap-target" onClick={onClearSelection}>
        Clear
      </button>
    </div>
  );
}
