import { useState } from "react";
import { STATUS_CLASS, EPIC_TABS } from "../constants.js";
import { archiveEpic, unarchiveEpic } from "../api.js";
import { JobTable } from "../components/JobTable.jsx";
import { ThinkingIndicator } from "../components/ThinkingIndicator.jsx";
import { ActivityFeed } from "../components/ActivityFeed.jsx";
import { DecisionsList } from "../components/DecisionsList.jsx";
import { IDEATE_ICON, ARCHITECT_ICON } from "../components/icons.js";

const TAB_LABEL = {
  jobs: "JOBS",
  ideate: "IDEATE",
  architect: "ARCHITECT",
  decisions: "DECISIONS",
};

// Depth of job i = 1 + max(depth of its deps), 0 if it has none. No graph
// library: this is a small memoized DFS over the plan's local job indices.
function computeDepths(jobs) {
  const memo = new Array(jobs.length).fill(null);
  function depthOf(i, visiting) {
    if (memo[i] != null) return memo[i];
    if (visiting.has(i)) return 0;
    visiting.add(i);
    const deps = jobs[i]?.depends_on || [];
    const d = deps.length === 0 ? 0 : 1 + Math.max(...deps.map((dep) => depthOf(dep, visiting)));
    visiting.delete(i);
    memo[i] = d;
    return d;
  }
  return jobs.map((_, i) => depthOf(i, new Set()));
}

function StreamingPanel({ text, toolEvents, onStop }) {
  return (
    <div className="suggest-panel epic-detail-panel">
      <ThinkingIndicator visible={!text} />
      {text && <div className="stream-text hint">{text}</div>}
      <ActivityFeed events={toolEvents} />
      <div className="suggest-actions">
        <button type="button" className="cancel-btn" onClick={onStop}>
          Stop
        </button>
      </div>
    </div>
  );
}

function IdeateTab({ epic, ideate }) {
  const panel = ideate.panel?.epicId === epic.id ? ideate.panel : null;

  if (!panel) {
    return (
      <div className="epic-detail-tab-empty">
        <p className="hint">Generate feature suggestions for this epic.</p>
        <button className="tap-target" onClick={() => ideate.open(epic)}>
          <IDEATE_ICON size={16} aria-hidden="true" />
          <span>Suggest features</span>
        </button>
      </div>
    );
  }

  if (ideate.streaming) {
    return (
      <StreamingPanel text={ideate.text} toolEvents={ideate.toolEvents} onStop={ideate.stop} />
    );
  }

  if (!panel.done) return null;

  return (
    <div className="suggest-panel epic-detail-panel">
      {panel.suggestions.length === 0 ? (
        <p className="hint">No suggestions returned.</p>
      ) : (
        <div className="suggest-list">
          {panel.suggestions.map((s, i) => (
            <label key={i} className="suggest-item">
              <input
                type="checkbox"
                checked={!!panel.checked[i]}
                onChange={() => ideate.toggleChecked(i)}
              />
              <span>
                <span className="suggest-title">{s.title}</span>
                <span className="suggest-desc">{s.description}</span>
              </span>
            </label>
          ))}
        </div>
      )}
      <div className="suggest-actions">
        <button
          disabled={!Object.values(panel.checked).some(Boolean)}
          onClick={() => ideate.createSelected(epic)}
        >
          Create selected
        </button>
        <button className="link" onClick={() => ideate.dismiss()}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

function ArchitectTab({ epic, architect }) {
  const panel = architect.panel?.epicId === epic.id ? architect.panel : null;

  if (!panel) {
    return (
      <div className="epic-detail-tab-empty">
        <p className="hint">Generate a job plan (DAG) for this epic.</p>
        <button className="tap-target" onClick={() => architect.open(epic)}>
          <ARCHITECT_ICON size={16} aria-hidden="true" />
          <span>Architect this epic</span>
        </button>
      </div>
    );
  }

  if (architect.streaming) {
    return (
      <StreamingPanel
        text={architect.text}
        toolEvents={architect.toolEvents}
        onStop={architect.stop}
      />
    );
  }

  if (!panel.done) return null;

  const depths = computeDepths(panel.jobs);
  const maxDepth = depths.length ? Math.max(...depths) : 0;
  const groups = [];
  for (let d = 0; d <= maxDepth; d++) {
    const group = panel.jobs.map((j, i) => ({ j, i })).filter(({ i }) => depths[i] === d);
    if (group.length > 0) groups.push({ depth: d, group });
  }

  return (
    <div className="suggest-panel epic-detail-panel">
      {panel.summary && <p className="hint">{panel.summary}</p>}
      {panel.jobs.length === 0 ? (
        <p className="hint">No plan returned.</p>
      ) : (
        <div className="suggest-list architect-plan-list">
          {groups.map(({ depth, group }) => (
            <div
              key={depth}
              className="architect-depth-group"
              style={{ marginLeft: `calc(var(--space-3h) * ${depth})` }}
            >
              <div className="architect-depth-label hint">Step {depth + 1}</div>
              {group.map(({ j, i }) => (
                <label key={i} className="suggest-item">
                  <input
                    type="checkbox"
                    checked={!!panel.checked[i]}
                    onChange={() => architect.toggleChecked(i)}
                  />
                  <span>
                    <span className="suggest-title">{j.title}</span>
                    <span className="suggest-desc">{j.idea}</span>
                    {Array.isArray(j.depends_on) && j.depends_on.length > 0 && (
                      <span className="dep-chips">
                        {j.depends_on.map((d) => (
                          <span key={d} className="dep-chip">
                            depends on: {panel.jobs[d]?.title || `#${d}`}
                          </span>
                        ))}
                      </span>
                    )}
                    {j.scope?.allowed_paths?.length > 0 && (
                      <span className="dep-chips">
                        <span className="dep-chip scope-badge">
                          {j.scope.allowed_paths.length} file
                          {j.scope.allowed_paths.length === 1 ? "" : "s"}
                        </span>
                      </span>
                    )}
                  </span>
                </label>
              ))}
            </div>
          ))}
        </div>
      )}
      <div className="suggest-actions">
        <button
          disabled={!Object.values(panel.checked).some(Boolean)}
          onClick={() => architect.createSelected(epic)}
        >
          Create selected
        </button>
        <button className="link" onClick={() => architect.dismiss()}>
          Dismiss
        </button>
      </div>
    </div>
  );
}

export function EpicDetail({
  epic,
  project,
  jobs,
  epics,
  tab,
  onTabChange,
  onNewJob,
  onOpenJob,
  ideate,
  architect,
  onChanged,
  setNote,
}) {
  const [archiving, setArchiving] = useState(false);

  async function handleArchive() {
    setArchiving(true);
    try {
      await archiveEpic(epic.id);
      onChanged();
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setArchiving(false);
    }
  }

  async function handleUnarchive() {
    setArchiving(true);
    try {
      await unarchiveEpic(epic.id);
      onChanged();
    } catch (err) {
      setNote("⚠️ " + err.message);
    } finally {
      setArchiving(false);
    }
  }

  return (
    <div className="epic-detail">
      <div className="epic-detail-header">
        <h2 className="epic-detail-name">{epic.name}</h2>
        <span className={`badge ${STATUS_CLASS[epic.status]}`}>{epic.status}</span>
        <span className="epic-detail-count">
          {epic.job_count} job{epic.job_count === 1 ? "" : "s"}
        </span>
        {epic.archived ? (
          <button className="cancel-btn tap-target" disabled={archiving} onClick={handleUnarchive}>
            Restore
          </button>
        ) : (
          <button className="cancel-btn tap-target" disabled={archiving} onClick={handleArchive}>
            Archive
          </button>
        )}
      </div>

      <div className="job-tabs epic-detail-tabs">
        <div className="job-tab-bar">
          {EPIC_TABS.map((t) => (
            <button
              key={t}
              className={`job-tab-btn tap-target${tab === t ? " active" : ""}`}
              onClick={() => onTabChange(t)}
            >
              {TAB_LABEL[t]}
            </button>
          ))}
        </div>

        {tab === "jobs" && (
          <JobTable
            jobs={jobs}
            epics={epics}
            project={project}
            selectedEpicId={epic.id}
            onNewJob={onNewJob}
            onOpenJob={onOpenJob}
            onChanged={onChanged}
          />
        )}
        {tab === "ideate" && <IdeateTab epic={epic} ideate={ideate} />}
        {tab === "architect" && <ArchitectTab epic={epic} architect={architect} />}
        {tab === "decisions" && <DecisionsList projectId={project.id} epicId={epic.id} />}
      </div>
    </div>
  );
}
