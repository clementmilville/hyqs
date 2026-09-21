import { useEffect, useState } from "react";
import { AgentsPanel } from "./AgentsPanel.jsx";
import { ApiTokensPanel } from "./ApiTokensPanel.jsx";
import { FixBudgetPanel } from "./FixBudgetPanel.jsx";
import { MembersPanel } from "./MembersPanel.jsx";
import { SettingsHealthRow } from "./SettingsHealthRow.jsx";
import { SlackIntegrationPanel } from "./SlackIntegrationPanel.jsx";
import {
  deleteProject,
  listAgents,
  listApiTokens,
  listProjectMembers,
  listProjectWebhooks,
  getSlackCredentialStatus,
} from "../api.js";

export function WorkspaceSettings({ project, setNote, onChanged, onDeleted }) {
  const [agents, setAgents] = useState([]);
  const [members, setMembers] = useState([]);
  const [tokens, setTokens] = useState([]);
  const [slackStatus, setSlackStatus] = useState(null);
  const [slackWebhooks, setSlackWebhooks] = useState([]);
  const [activeEdit, setActiveEdit] = useState(null);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmText, setConfirmText] = useState("");

  const reload = () => {
    listAgents(project.id)
      .then(setAgents)
      .catch(() => {});
    listProjectMembers(project.id)
      .then(setMembers)
      .catch(() => {});
    listApiTokens(project.id)
      .then(setTokens)
      .catch(() => {});
    getSlackCredentialStatus(project.id)
      .then(setSlackStatus)
      .catch(() => {});
    listProjectWebhooks(project.id)
      .then(setSlackWebhooks)
      .catch(() => {});
  };

  useEffect(() => {
    reload();
  }, [project.id]);

  const handleDelete = () => {
    deleteProject(project.id)
      .then(() => onDeleted?.())
      .catch((err) => setNote?.(err.message || "Failed to delete project"));
  };

  return (
    <div className="settings-tab">
      <h2 className="settings-title">{project.name} — Settings</h2>
      <SettingsHealthRow
        agents={agents}
        project={project}
        members={members}
        tokens={tokens}
        slackConfigured={!!slackStatus?.configured}
        slackChannelCount={slackWebhooks.filter((w) => w.kind === "slack").length}
        activeEdit={activeEdit}
        onEdit={setActiveEdit}
      />
      {activeEdit === "agents" && (
        <AgentsPanel headless projectId={project.id} onChanged={reload} />
      )}
      {activeEdit === "budget" && (
        <FixBudgetPanel project={project} setNote={setNote} onChanged={() => onChanged?.()} />
      )}
      {activeEdit === "members" && (
        <MembersPanel projectId={project.id} setNote={setNote} onChanged={reload} />
      )}
      {activeEdit === "tokens" && (
        <ApiTokensPanel projectId={project.id} setNote={setNote} onChanged={reload} />
      )}
      {activeEdit === "slack" && (
        <SlackIntegrationPanel projectId={project.id} setNote={setNote} onChanged={reload} />
      )}
      <section className="settings-section danger-zone">
        <h3>Danger zone</h3>
        {!confirmOpen ? (
          <button className="btn-ghost-danger" onClick={() => setConfirmOpen(true)}>
            Delete project
          </button>
        ) : (
          <div className="danger-zone-confirm">
            <p className="hint">
              Type <strong>{project.name}</strong> to confirm. This cannot be undone.
            </p>
            <input
              className="danger-zone-confirm-input"
              value={confirmText}
              onChange={(e) => setConfirmText(e.target.value)}
              placeholder={project.name}
              autoFocus
            />
            <div className="danger-zone-confirm-actions">
              <button
                className="btn-danger"
                disabled={confirmText !== project.name}
                onClick={handleDelete}
              >
                Confirm delete
              </button>
              <button
                className="btn-secondary"
                onClick={() => {
                  setConfirmOpen(false);
                  setConfirmText("");
                }}
              >
                Cancel
              </button>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
