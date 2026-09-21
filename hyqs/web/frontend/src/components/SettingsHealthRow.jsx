import { GatedAction } from "../context.js";

const DEFAULT_FIX_BUDGET = 3;

function initials(member) {
  const name = member.user_display_name || member.user_email || "";
  return name.slice(0, 2).toUpperCase() || "?";
}

export function SettingsHealthRow({
  agents,
  project,
  members,
  tokens = [],
  slackConfigured = false,
  slackChannelCount = 0,
  activeEdit,
  onEdit,
}) {
  const capacity = agents.filter((a) => a.enabled).reduce((n, a) => n + a.max_concurrency, 0);

  const r = 22;
  const circ = 2 * Math.PI * r;
  const fraction = capacity / Math.max(capacity, 8);
  const offset = circ * (1 - fraction);

  const effective = project.max_fix_attempts ?? DEFAULT_FIX_BUDGET;
  const budgetPct = Math.min((effective / 10) * 100, 100);

  const previewMembers = members.slice(0, 3);

  const toggle = (key) => onEdit(activeEdit === key ? null : key);

  return (
    <div className="settings-health-row">
      <div className={`health-tile${activeEdit === "agents" ? " health-tile-active" : ""}`}>
        <div className="health-tile-title">Agents</div>
        <svg className="health-tile-ring" width="60" height="60" viewBox="0 0 60 60">
          <circle cx="30" cy="30" r={r} fill="none" stroke="var(--line)" strokeWidth="5" />
          <circle
            cx="30"
            cy="30"
            r={r}
            fill="none"
            stroke="var(--accent)"
            strokeWidth="5"
            strokeDasharray={circ}
            strokeDashoffset={offset}
            transform="rotate(-90 30 30)"
          />
        </svg>
        <div className="health-tile-label">
          {agents.length} agent{agents.length === 1 ? "" : "s"} · {capacity} parallel job
          {capacity === 1 ? "" : "s"}
        </div>
        <GatedAction require="edit_agents">
          <button className="health-tile-edit" onClick={() => toggle("agents")}>
            {activeEdit === "agents" ? "Done" : "Edit"}
          </button>
        </GatedAction>
      </div>

      <div className={`health-tile${activeEdit === "budget" ? " health-tile-active" : ""}`}>
        <div className="health-tile-title">Fix Budget</div>
        <div className="health-tile-bar-wrap">
          <div className="health-tile-bar" style={{ width: `${budgetPct}%` }} />
        </div>
        <div className="health-tile-label">effective: {effective}</div>
        <GatedAction require="edit_project">
          <button className="health-tile-edit" onClick={() => toggle("budget")}>
            {activeEdit === "budget" ? "Done" : "Edit"}
          </button>
        </GatedAction>
      </div>

      <div className={`health-tile${activeEdit === "members" ? " health-tile-active" : ""}`}>
        <div className="health-tile-title">Members</div>
        <div className="health-tile-members">
          <span className="health-tile-count">{members.length}</span>
          <div className="health-tile-initials">
            {previewMembers.map((m) => (
              <span key={m.id || m.user_id} className="initial-bubble">
                {initials(m)}
              </span>
            ))}
          </div>
        </div>
        <GatedAction require="manage_members">
          <button className="health-tile-edit" onClick={() => toggle("members")}>
            {activeEdit === "members" ? "Done" : "Edit"}
          </button>
        </GatedAction>
      </div>

      <div className={`health-tile${activeEdit === "tokens" ? " health-tile-active" : ""}`}>
        <div className="health-tile-title">API Tokens</div>
        <div className="health-tile-count">{tokens.filter((t) => !t.revoked_at).length}</div>
        <div className="health-tile-label">scoped access tokens</div>
        <GatedAction require="manage_api_tokens">
          <button className="health-tile-edit" onClick={() => toggle("tokens")}>
            {activeEdit === "tokens" ? "Done" : "Edit"}
          </button>
        </GatedAction>
      </div>

      <div className={`health-tile${activeEdit === "slack" ? " health-tile-active" : ""}`}>
        <div className="health-tile-title">Slack</div>
        <div>
          <span className={`badge ${slackConfigured ? "ok" : "bad"}`}>
            {slackConfigured ? "Configured" : "Not connected"}
          </span>
        </div>
        <div className="health-tile-label">
          {slackChannelCount} channel{slackChannelCount === 1 ? "" : "s"}
        </div>
        <GatedAction require="manage_webhooks">
          <button className="health-tile-edit" onClick={() => toggle("slack")}>
            {activeEdit === "slack" ? "Done" : "Edit"}
          </button>
        </GatedAction>
      </div>
    </div>
  );
}
