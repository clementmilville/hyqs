import { useEffect, useState } from "react";
import { GatedAction } from "../context.js";
import {
  listAgents,
  agentStats,
  getAgentMeta,
  createAgent,
  updateAgent,
  deleteAgent,
} from "../api.js";
import { AgentRow } from "./AgentRow.jsx";
import { useToast } from "./Toast.jsx";

export function AgentsPanel({ projectId, headless = false, onChanged }) {
  const [agents, setAgents] = useState([]);
  const [meta, setMeta] = useState({ providers: [], tasks: [] });
  const [statsMap, setStatsMap] = useState({});
  const [open, setOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newProvider, setNewProvider] = useState("claude");
  const toast = useToast();

  const reload = () => {
    listAgents(projectId)
      .then(setAgents)
      .catch(() => {});
    agentStats(projectId)
      .then((list) => {
        const m = {};
        for (const s of list) m[s.agent_id] = s;
        setStatsMap(m);
      })
      .catch(() => {});
  };
  useEffect(() => {
    reload();
    getAgentMeta()
      .then((m) => {
        setMeta(m);
        setNewProvider(m.providers[0] || "claude");
      })
      .catch(() => {});
  }, [projectId]);

  const change = async (id, p) => {
    try {
      const a = await updateAgent(id, p);
      setAgents((xs) => xs.map((x) => (x.id === id ? a : x)));
      onChanged?.();
      toast.success("Agent updated");
    } catch (err) {
      toast.error(err.message);
    }
  };
  const add = async (e) => {
    e.preventDefault();
    const name = newName.trim();
    if (!name) return;
    try {
      await createAgent(projectId, {
        name,
        provider: newProvider,
        allowed_tasks: meta.tasks,
        max_concurrency: 1,
        enabled: true,
      });
      setNewName("");
      reload();
      onChanged?.();
      toast.success(`Agent "${name}" created`);
    } catch (err) {
      toast.error(err.message);
    }
  };
  const del = async (id) => {
    try {
      await deleteAgent(id);
      reload();
      onChanged?.();
      toast.success("Agent deleted");
    } catch (err) {
      toast.error(err.message);
    }
  };

  const capacity = agents.filter((a) => a.enabled).reduce((n, a) => n + a.max_concurrency, 0);
  return (
    <section className={`agents${headless ? " agents-headless" : ""}`}>
      {!headless && (
        <div className="agents-head clickable" onClick={() => setOpen((o) => !o)}>
          <span className="agents-title">🤖 Agents</span>
          <span className="agents-sum">
            {agents.length} agent{agents.length === 1 ? "" : "s"} · up to {capacity} parallel job
            {capacity === 1 ? "" : "s"}
          </span>
          <span className="expand">{open ? "▾" : "▸"}</span>
        </div>
      )}
      {(headless || open) && (
        <div className="agents-body">
          <div className="agent-row agent-hd">
            <span>name</span>
            <span>provider</span>
            <span>model</span>
            <span>tasks</span>
            <span>parallel</span>
            <span>on</span>
            <span></span>
          </div>
          {agents.map((a) => (
            <AgentRow
              key={a.id}
              agent={a}
              tasks={meta.tasks}
              providers={meta.providers}
              onChange={change}
              onDelete={del}
              stats={statsMap[a.id]}
            />
          ))}
          {agents.length === 0 && (
            <p className="hint">No agents — this project won't build until you add one.</p>
          )}
          <GatedAction require="edit_agents">
            <form className="agent-add" onSubmit={add}>
              <input
                value={newName}
                onChange={(e) => setNewName(e.target.value)}
                placeholder="New agent name…"
              />
              <select value={newProvider} onChange={(e) => setNewProvider(e.target.value)}>
                {meta.providers.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </select>
              <button>Add agent</button>
            </form>
          </GatedAction>
          <p className="hint agent-note">
            Test &amp; merge are deterministic — they run on any free worker regardless of these
            agents.
          </p>
        </div>
      )}
    </section>
  );
}
