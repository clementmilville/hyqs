import { useEffect, useState } from "react";

export function FilterPresets({ projectId, currentFilters, onLoad }) {
  const storageKey = `work_presets_${projectId}`;
  const [presets, setPresets] = useState([]);
  const [name, setName] = useState("");
  const [selectedIdx, setSelectedIdx] = useState("");

  useEffect(() => {
    try {
      setPresets(JSON.parse(localStorage.getItem(storageKey) || "[]"));
    } catch {
      setPresets([]);
    }
  }, [storageKey]);

  function save() {
    if (!name.trim()) return;
    const updated = [...presets, { name: name.trim(), state: currentFilters }];
    localStorage.setItem(storageKey, JSON.stringify(updated));
    setPresets(updated);
    setName("");
  }

  function load() {
    if (selectedIdx === "") return;
    onLoad(presets[+selectedIdx].state);
  }

  function remove() {
    if (selectedIdx === "") return;
    const updated = presets.filter((_, j) => j !== +selectedIdx);
    localStorage.setItem(storageKey, JSON.stringify(updated));
    setPresets(updated);
    setSelectedIdx("");
  }

  return (
    <div className="filter-presets">
      <input
        className="filter-presets-name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="Save preset…"
        aria-label="Saved view name"
        onKeyDown={(e) => e.key === "Enter" && save()}
      />
      <button
        className="btn-secondary filter-presets-save tap-target"
        disabled={!name.trim()}
        onClick={save}
        aria-label="Save current view"
      >
        Save
      </button>
      {presets.length > 0 && (
        <>
          <select
            className="filter-presets-select"
            value={selectedIdx}
            onChange={(e) => setSelectedIdx(e.target.value)}
            aria-label="Saved views"
          >
            <option value="">Presets…</option>
            {presets.map((p, i) => (
              <option key={i} value={i}>
                {p.name}
              </option>
            ))}
          </select>
          <button
            className="btn-secondary tap-target"
            disabled={selectedIdx === ""}
            onClick={load}
            aria-label="Load saved view"
          >
            Load
          </button>
          <button
            className="cancel-btn tap-target"
            disabled={selectedIdx === ""}
            onClick={remove}
            title="Delete preset"
            aria-label="Delete saved view"
          >
            ×
          </button>
        </>
      )}
    </div>
  );
}
