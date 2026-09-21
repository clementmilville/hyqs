import { useEffect, useRef, useState } from "react";
import { MoreVertical, Compass } from "lucide-react";
import { STATUS_CLASS } from "../constants.js";
import { ago } from "../utils.js";
import { createEpic, archiveEpic, unarchiveEpic } from "../api.js";
import { EmptyState } from "../components/EmptyState.jsx";
import { IDEATE_ICON, ARCHITECT_ICON } from "../components/icons.js";

// Attention-first: epics with failing jobs surface before running ones,
// which surface before everything else (CONVENTIONS.md §9.3).
function attentionRank(progress) {
  if (progress.failed > 0) return 0;
  if (progress.running > 0) return 1;
  return 2;
}

// Archive/Restore lives behind a menu, gated by a confirm — never the
// loudest control on the row (see CONVENTIONS.md §9d).
function EpicRowMenu({ epic, onChanged, setNote }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    function handleClick(e) {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  async function handleToggleArchive() {
    const verb = epic.archived ? "Restore" : "Archive";
    if (!window.confirm(`${verb} epic "${epic.name}"?`)) return;
    setBusy(true);
    try {
      if (epic.archived) await unarchiveEpic(epic.id);
      else await archiveEpic(epic.id);
      onChanged();
      setOpen(false);
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="epic-row-menu-wrap" ref={ref}>
      <button
        className="epic-row-menu-btn tap-target"
        onClick={(e) => {
          e.stopPropagation();
          setOpen((v) => !v);
        }}
        aria-label="Epic actions"
        title="More actions"
      >
        <MoreVertical size={16} />
      </button>
      {open && (
        <div className="epic-row-menu-dropdown">
          <button
            className="epic-row-menu-item danger tap-target"
            disabled={busy}
            onClick={(e) => {
              e.stopPropagation();
              handleToggleArchive();
            }}
          >
            {epic.archived ? "Restore" : "Archive"}
          </button>
        </div>
      )}
    </div>
  );
}

export function EpicsIndex({
  epics,
  project,
  onChanged,
  setNote,
  showArchivedEpics,
  setShowArchivedEpics,
  onOpenEpic,
  onOpenIdeate,
  onOpenArchitect,
  ideateBusyEpicId,
  architectBusyEpicId,
}) {
  const [epicName, setEpicName] = useState("");
  const [creating, setCreating] = useState(false);

  const visibleEpics = (showArchivedEpics ? epics : epics.filter((e) => !e.archived))
    .slice()
    .sort((a, b) => attentionRank(a.status_counts) - attentionRank(b.status_counts));

  async function addEpic(e) {
    e.preventDefault();
    if (!epicName.trim()) return;
    setCreating(true);
    try {
      await createEpic(project.id, epicName.trim(), "");
      setEpicName("");
      onChanged();
      setNote("Epic created");
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setCreating(false);
    }
  }

  return (
    <div className="epics-index">
      <div className="epics-index-toolbar">
        <form className="epics-index-create" onSubmit={addEpic}>
          <input
            value={epicName}
            onChange={(e) => setEpicName(e.target.value)}
            placeholder="New epic…"
            aria-label="New epic name"
          />
          <button type="submit" className="tap-target" disabled={creating}>
            {creating ? "Adding…" : "New epic"}
          </button>
        </form>
        <button
          className={`epic-sidebar-toggle tap-target${showArchivedEpics ? " active" : ""}`}
          onClick={() => setShowArchivedEpics((v) => !v)}
        >
          {showArchivedEpics ? "Hide archived" : "Show archived"}
        </button>
      </div>

      {visibleEpics.length === 0 ? (
        <EmptyState
          icon={Compass}
          title="No epics yet"
          hint="Create one above to start planning."
        />
      ) : (
        <div className="epics-index-list">
          {visibleEpics.map((ep) => {
            const progress = ep.status_counts;
            return (
              <div key={ep.id} className={`epics-index-row${ep.archived ? " archived" : ""}`}>
                <button
                  className="epics-index-open tap-target"
                  onClick={() => onOpenEpic(ep.id)}
                  title="Open epic"
                >
                  <span className="epics-index-name" title={ep.name}>
                    {ep.name}
                  </span>
                  <span className={`badge ${STATUS_CLASS[ep.status]}`}>{ep.status}</span>
                </button>
                <div className="epics-index-meta">
                  <span className="epics-index-progress">
                    {progress.done} done · {progress.running} running · {progress.failed} failed
                  </span>
                  <span className="epics-index-activity">{ago(ep.last_activity)}</span>
                </div>
                <div className="epics-index-actions">
                  {!ep.archived && (
                    <button
                      className="link suggest-btn tap-target"
                      disabled={ideateBusyEpicId === ep.id}
                      onClick={() => onOpenIdeate(ep)}
                      title="Suggest features"
                    >
                      {ideateBusyEpicId === ep.id ? (
                        "…"
                      ) : (
                        <>
                          <IDEATE_ICON size={16} aria-hidden="true" />
                          <span>Ideate</span>
                        </>
                      )}
                    </button>
                  )}
                  {!ep.archived && (
                    <button
                      className="link architect-btn tap-target"
                      disabled={architectBusyEpicId === ep.id}
                      onClick={() => onOpenArchitect(ep)}
                      title="Architect: plan this epic"
                    >
                      {architectBusyEpicId === ep.id ? (
                        "…"
                      ) : (
                        <>
                          <ARCHITECT_ICON size={16} aria-hidden="true" />
                          <span>Architect</span>
                        </>
                      )}
                    </button>
                  )}
                  <EpicRowMenu epic={ep} onChanged={onChanged} setNote={setNote} />
                </div>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
