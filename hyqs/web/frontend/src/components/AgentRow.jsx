import { GatedAction } from "../context.js";

const TASK_LABEL = { plan: "Plan", build: "Build", fix: "Fix", review: "Review" };

export function AgentRow({ agent, tasks, providers, onChange, onDelete, stats }) {
  const patch = (p) => onChange(agent.id, p);
  const toggleTask = (t) => {
    const has = agent.allowed_tasks.includes(t);
    patch({
      allowed_tasks: has ? agent.allowed_tasks.filter((x) => x !== t) : [...agent.allowed_tasks, t],
    });
  };
  const hasData = stats && (stats.avg_duration_s != null || stats.total_cost_usd > 0);
  return (
    <div className={`agent-row ${agent.enabled ? "" : "off"}`}>
      <input
        className="agent-name"
        defaultValue={agent.name}
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v && v !== agent.name) patch({ name: v });
        }}
      />
      <select value={agent.provider} onChange={(e) => patch({ provider: e.target.value })}>
        {providers.map((p) => (
          <option key={p} value={p}>
            {p}
          </option>
        ))}
      </select>
      <input
        className="agent-model"
        defaultValue={agent.model}
        placeholder="default"
        onBlur={(e) => {
          const v = e.target.value.trim();
          if (v !== agent.model) patch({ model: v });
        }}
      />
      <span className="agent-tasks">
        {tasks.map((t) => (
          <label key={t} className={agent.allowed_tasks.includes(t) ? "on" : ""}>
            <input
              type="checkbox"
              checked={agent.allowed_tasks.includes(t)}
              onChange={() => toggleTask(t)}
            />
            {TASK_LABEL[t] || t}
          </label>
        ))}
      </span>
      <span className="agent-conc" title="max jobs this agent runs at once">
        ×
        <input
          type="number"
          min="1"
          value={agent.max_concurrency}
          onChange={(e) =>
            patch({ max_concurrency: Math.max(1, parseInt(e.target.value || "1", 10)) })
          }
        />
      </span>
      <label className="agent-on" title="enabled">
        <input
          type="checkbox"
          checked={agent.enabled}
          onChange={(e) => patch({ enabled: e.target.checked })}
        />
      </label>
      <GatedAction require="edit_agents">
        <button className="link del" title="remove agent" onClick={() => onDelete(agent.id)}>
          ✕
        </button>
      </GatedAction>
      {hasData && (
        <div className="agent-stats">
          <span className="stat-chip">${stats.total_cost_usd.toFixed(4)}</span>
          <span className="stat-chip">{(stats.avg_duration_s ?? 0).toFixed(1)} s avg</span>
          <span className="stat-chip">{Math.round((stats.fix_rate || 0) * 100)} % fix</span>
        </div>
      )}
    </div>
  );
}
