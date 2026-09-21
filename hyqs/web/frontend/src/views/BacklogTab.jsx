import { useContext, useEffect, useState } from "react";
import { ClipboardList } from "lucide-react";
import { GatedAction, ProjectContext } from "../context.js";
import { EmptyState } from "../components/EmptyState.jsx";
import { PageState } from "../components/PageState.jsx";
import { useToast } from "../components/Toast.jsx";
import { IdeationPanel } from "../components/IdeationPanel.jsx";
import { usePageData } from "../hooks/usePageData.js";
import {
  listBacklogItems,
  createBacklogItem,
  voteBacklogItem,
  patchBacklogItem,
  refineBacklogItems,
  createBatchJobs,
} from "../api.js";

const TYPE_BADGE_CLASS = {
  feature: "badge-feature",
  bug: "badge-bug",
  chore: "badge-chore",
  idea: "badge-idea",
};

const STATUS_BADGE_CLASS = {
  completed: "badge ok",
  converted: "badge run",
};

const TERMINAL_STATUSES = ["declined", "completed", "converted"];

function isTerminal(status) {
  return TERMINAL_STATUSES.includes(status);
}

function BacklogRow({ item, checked, onToggle, voting, onVote, onPatch, onRefineThis }) {
  const [hintEditing, setHintEditing] = useState(false);
  const [hintDraft, setHintDraft] = useState(item.epic_hint || "");

  function startEditHint() {
    setHintDraft(item.epic_hint || "");
    setHintEditing(true);
  }

  async function commitHint() {
    setHintEditing(false);
    if (hintDraft !== (item.epic_hint || "")) {
      await onPatch(item.id, { epic_hint: hintDraft });
    }
  }

  function handleHintKey(e) {
    if (e.key === "Enter") commitHint();
    if (e.key === "Escape") setHintEditing(false);
  }

  return (
    <tr>
      <td>
        <input
          type="checkbox"
          aria-label={`Select ${item.title}`}
          checked={checked}
          disabled={isTerminal(item.status)}
          onChange={() => onToggle(item.id)}
        />
      </td>
      <td data-label="Type">
        <span className={`type-badge ${TYPE_BADGE_CLASS[item.type] ?? ""}`}>{item.type}</span>
      </td>
      <td data-label="Title">{item.title}</td>
      <td data-label="Proposer" className="proposer-cell">
        {item.proposed_by}
      </td>
      <td data-label="Votes">
        <GatedAction require="propose_backlog">
          <button className="vote-btn tap-target" disabled={voting} onClick={() => onVote(item.id)}>
            ▲ {item.votes}
          </button>
        </GatedAction>
      </td>
      <td data-label="Status">
        <span className={STATUS_BADGE_CLASS[item.status] ?? "badge"}>{item.status}</span>
      </td>
      <td>
        {item.status === "converted" && item.linked_job_ids?.length > 0 && (
          <span className="converted-jobs">
            Jobs:{" "}
            {item.linked_job_ids.map((jid, i) => (
              <span key={jid}>
                {i > 0 && ", "}
                <a href={`#workspace/${item.project_id}/work`} className="job-link">
                  #{jid}
                </a>
              </span>
            ))}
          </span>
        )}
        {!isTerminal(item.status) && (
          <GatedAction require="triage_backlog">
            <div className="triage-controls">
              <button
                className="triage-accept tap-target"
                onClick={() => onPatch(item.id, { status: "accepted" })}
              >
                Accept
              </button>
              <button
                className="triage-decline tap-target"
                onClick={() => onPatch(item.id, { status: "declined" })}
              >
                Decline
              </button>
              {hintEditing ? (
                <input
                  className="epic-hint-input"
                  value={hintDraft}
                  onChange={(e) => setHintDraft(e.target.value)}
                  onBlur={commitHint}
                  onKeyDown={handleHintKey}
                  autoFocus
                  placeholder="Epic hint…"
                />
              ) : (
                <span className="epic-hint-display tap-target" onClick={startEditHint}>
                  {item.epic_hint || "Epic hint…"}
                </span>
              )}
            </div>
          </GatedAction>
        )}
        {(item.status === "new" || item.status === "accepted") && (
          <GatedAction require="propose_backlog">
            <button
              className="mark-completed-btn tap-target"
              onClick={() => onPatch(item.id, { status: "completed" })}
            >
              Mark Completed
            </button>
          </GatedAction>
        )}
        {!isTerminal(item.status) && (
          <GatedAction require="queue_job">
            <button className="refine-this-btn tap-target" onClick={() => onRefineThis(item.id)}>
              Refine this
            </button>
          </GatedAction>
        )}
      </td>
    </tr>
  );
}

export function BacklogTab({ projectId }) {
  const toast = useToast();
  const { project } = useContext(ProjectContext);
  const [items, setItems] = useState([]);
  const [statusFilter, setStatusFilter] = useState("");
  const [typeFilter, setTypeFilter] = useState("");
  const [composeOpen, setComposeOpen] = useState(false);
  const [form, setForm] = useState({ title: "", body: "", type: "idea" });
  const [submitting, setSubmitting] = useState(false);
  const [votingIds, setVotingIds] = useState(new Set());
  const [selectedIds, setSelectedIds] = useState(new Set());
  // null | { sessionId, refineIds, loading, panel }
  // panel: { options, selectedIdx, editedTitle, editedDesc, refineNote }
  const [refineState, setRefineState] = useState(null);

  const {
    data: fetchedItems,
    loading,
    forbidden,
    error,
    retry,
  } = usePageData(
    () => listBacklogItems(projectId, statusFilter || undefined, typeFilter || undefined),
    [projectId, statusFilter, typeFilter]
  );

  useEffect(() => {
    if (fetchedItems) setItems(fetchedItems);
  }, [fetchedItems]);

  async function handlePropose(e) {
    e.preventDefault();
    setSubmitting(true);
    try {
      const item = await createBacklogItem(
        projectId,
        form.title.trim(),
        form.body.trim(),
        form.type
      );
      setItems((prev) => [item, ...prev]);
      setForm({ title: "", body: "", type: "idea" });
      setComposeOpen(false);
      toast.success("Item proposed");
    } catch (err) {
      toast.error(err.message || "Failed to propose item");
    } finally {
      setSubmitting(false);
    }
  }

  async function handleVote(itemId) {
    setVotingIds((s) => new Set([...s, itemId]));
    try {
      const { votes } = await voteBacklogItem(itemId);
      setItems((prev) => prev.map((it) => (it.id === itemId ? { ...it, votes } : it)));
    } catch (err) {
      toast.error(err.message || "Failed to vote");
    } finally {
      setVotingIds((s) => {
        const next = new Set(s);
        next.delete(itemId);
        return next;
      });
    }
  }

  async function handlePatch(itemId, patch) {
    try {
      const updated = await patchBacklogItem(itemId, patch);
      setItems((prev) => prev.map((it) => (it.id === itemId ? updated : it)));
      toast.success("Updated");
    } catch (err) {
      toast.error(err.message || "Failed to update");
    }
  }

  function handleToggle(itemId) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(itemId)) next.delete(itemId);
      else next.add(itemId);
      return next;
    });
  }

  const selectableItems = items.filter((it) => !isTerminal(it.status));
  const allFilteredSelected =
    selectableItems.length > 0 && selectableItems.every((it) => selectedIds.has(it.id));

  function handleSelectAll() {
    if (allFilteredSelected) {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        selectableItems.forEach((it) => next.delete(it.id));
        return next;
      });
    } else {
      setSelectedIds((prev) => {
        const next = new Set(prev);
        selectableItems.forEach((it) => next.add(it.id));
        return next;
      });
    }
  }

  async function openRefinePanel(itemIds) {
    setRefineState({ sessionId: null, refineIds: itemIds, loading: true, panel: null });
    try {
      const resp = await refineBacklogItems(projectId, itemIds);
      const options = (resp.proposed_jobs || []).map((j, i) => ({
        id: i,
        title: j.title,
        description: j.description || "",
        acceptance: j.acceptance_criteria || "",
      }));
      setRefineState({
        sessionId: resp.session_id,
        refineIds: itemIds,
        loading: false,
        panel: {
          options,
          selectedIdx: options.length === 1 ? 0 : null,
          editedTitle: options.length === 1 ? options[0].title : "",
          editedDesc: options.length === 1 ? options[0].description : "",
          refineNote: "",
        },
      });
    } catch (err) {
      toast.error(err.message || "Refinement failed");
      setRefineState(null);
    }
  }

  async function handleRegenerate() {
    if (!refineState) return;
    const { refineIds, panel } = refineState;
    setRefineState((s) => (s ? { ...s, loading: true } : s));
    try {
      const resp = await refineBacklogItems(projectId, refineIds);
      const options = (resp.proposed_jobs || []).map((j, i) => ({
        id: i,
        title: j.title,
        description: j.description || "",
        acceptance: j.acceptance_criteria || "",
      }));
      setRefineState({
        sessionId: resp.session_id,
        refineIds,
        loading: false,
        panel: {
          options,
          selectedIdx: null,
          editedTitle: "",
          editedDesc: "",
          refineNote: panel?.refineNote ?? "",
        },
      });
    } catch (err) {
      toast.error(err.message || "Regeneration failed");
      setRefineState((s) => (s ? { ...s, loading: false } : s));
    }
  }

  async function handleConfirm() {
    if (!refineState?.panel) return;
    const { panel, sessionId } = refineState;
    if (panel.selectedIdx === null || !panel.editedTitle.trim()) return;
    const opt = panel.options[panel.selectedIdx];
    try {
      await createBatchJobs(
        project?.repo_path ?? "",
        [
          {
            title: panel.editedTitle,
            description: panel.editedDesc,
            acceptance_criteria: opt.acceptance,
            depends_on: [],
          },
        ],
        sessionId
      );
      toast.success("Job queued");
      setRefineState(null);
      setSelectedIds(new Set());
      const updated = await listBacklogItems(
        projectId,
        statusFilter || undefined,
        typeFilter || undefined
      );
      setItems(updated);
    } catch (err) {
      toast.error(err.message || "Failed to create job");
    }
  }

  function handleCancelRefine() {
    setRefineState(null);
    setSelectedIds(new Set());
  }

  function updatePanel(patch) {
    setRefineState((s) => (s ? { ...s, panel: { ...s.panel, ...patch } } : s));
  }

  return (
    <div className="backlog-tab">
      <div className="backlog-toolbar">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label="Filter by status"
        >
          <option value="">All statuses</option>
          <option value="new">New</option>
          <option value="accepted">Accepted</option>
          <option value="declined">Declined</option>
          <option value="completed">Completed</option>
          <option value="converted">Converted</option>
        </select>
        <select
          value={typeFilter}
          onChange={(e) => setTypeFilter(e.target.value)}
          aria-label="Filter by type"
        >
          <option value="">All types</option>
          <option value="feature">Feature</option>
          <option value="bug">Bug</option>
          <option value="chore">Chore</option>
          <option value="idea">Idea</option>
        </select>
        <GatedAction require="queue_job">
          <button
            className="refine-btn tap-target"
            disabled={selectedIds.size === 0}
            onClick={() => openRefinePanel([...selectedIds])}
          >
            Refine selected ({selectedIds.size})
          </button>
        </GatedAction>
        <GatedAction require="propose_backlog">
          <button className="compose-toggle tap-target" onClick={() => setComposeOpen((v) => !v)}>
            {composeOpen ? "Cancel" : "+ Propose"}
          </button>
        </GatedAction>
      </div>

      {composeOpen && (
        <form className="backlog-compose" onSubmit={handlePropose}>
          <input
            className="compose-title"
            placeholder="Title"
            required
            value={form.title}
            onChange={(e) => setForm((f) => ({ ...f, title: e.target.value }))}
          />
          <textarea
            className="compose-body"
            placeholder="Description (optional)"
            value={form.body}
            onChange={(e) => setForm((f) => ({ ...f, body: e.target.value }))}
          />
          <select
            className="compose-type"
            value={form.type}
            onChange={(e) => setForm((f) => ({ ...f, type: e.target.value }))}
          >
            <option value="feature">Feature</option>
            <option value="bug">Bug</option>
            <option value="chore">Chore</option>
            <option value="idea">Idea</option>
          </select>
          <GatedAction require="propose_backlog">
            <button type="submit" className="tap-target" disabled={submitting}>
              {submitting ? "Proposing…" : "Propose"}
            </button>
          </GatedAction>
        </form>
      )}

      {refineState && (
        <div className="refine-panel-wrapper">
          {refineState.loading ? (
            <p className="hint">Analyzing backlog items…</p>
          ) : (
            <IdeationPanel
              panel={refineState.panel}
              confirmDisabled={false}
              onSelect={(i) => {
                const opt = refineState.panel.options[i];
                updatePanel({
                  selectedIdx: i,
                  editedTitle: opt.title,
                  editedDesc: opt.description,
                });
              }}
              onEditTitle={(v) => updatePanel({ editedTitle: v })}
              onEditDesc={(v) => updatePanel({ editedDesc: v })}
              onRefineChange={(v) => updatePanel({ refineNote: v })}
              onRegenerate={handleRegenerate}
              onConfirm={handleConfirm}
              onCancel={handleCancelRefine}
            />
          )}
        </div>
      )}

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        {items.length === 0 ? (
          <EmptyState
            icon={ClipboardList}
            title="No backlog items"
            hint="Be the first to propose one."
          />
        ) : (
          <div className="backlog-table-wrap table-scroll">
            <table className="backlog-table">
              <thead>
                <tr>
                  <th>
                    <input
                      type="checkbox"
                      aria-label="Select all"
                      checked={allFilteredSelected}
                      onChange={handleSelectAll}
                    />
                  </th>
                  <th>Type</th>
                  <th>Title</th>
                  <th>Proposer</th>
                  <th>Votes</th>
                  <th>Status</th>
                  <th></th>
                </tr>
              </thead>
              <tbody>
                {items.map((item) => (
                  <BacklogRow
                    key={item.id}
                    item={item}
                    checked={selectedIds.has(item.id)}
                    onToggle={handleToggle}
                    voting={votingIds.has(item.id)}
                    onVote={handleVote}
                    onPatch={handlePatch}
                    onRefineThis={(id) => openRefinePanel([id])}
                  />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </PageState>
    </div>
  );
}
