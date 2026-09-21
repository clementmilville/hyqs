import { useEffect, useRef, useState } from "react";
import { useRole } from "../context.js";
import { canSeeAdminTab } from "../constants.js";

const WS_TABS = [
  { id: "dashboard", label: "Dashboard" },
  { id: "work", label: "Work" },
  { id: "insights", label: "Insights" },
  { id: "performance", label: "Performance" },
];

const ADMIN_TABS = [
  { id: "command-center", label: "Command Center" },
  { id: "projects", label: "Projects" },
  { id: "new-project", label: "New Project" },
  { id: "providers", label: "Providers" },
  { id: "access", label: "Access" },
];

function Highlighted({ text, query }) {
  if (!query) return text;
  const lo = text.toLowerCase();
  const qi = query.toLowerCase();
  const idx = lo.indexOf(qi);
  if (idx === -1) return text;
  return (
    <>
      {text.slice(0, idx)}
      <mark className="cmd-highlight">{text.slice(idx, idx + query.length)}</mark>
      {text.slice(idx + query.length)}
    </>
  );
}

export function CommandPalette({
  open,
  onClose,
  projects,
  currentProjectId,
  onNavWorkspace,
  onNavAdmin,
}) {
  const { can } = useRole();
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef(null);

  const commands = [];

  for (const p of projects) {
    commands.push({
      id: `project-${p.id}`,
      label: `Switch to ${p.name}`,
      group: "Projects",
      action: () => {
        onNavWorkspace(p.id, "dashboard");
        onClose();
      },
    });
  }

  commands.push({
    id: "create-job",
    label: "Create job",
    group: "Actions",
    action: () => {
      onNavWorkspace(currentProjectId, "work");
      onClose();
    },
  });

  for (const t of WS_TABS) {
    commands.push({
      id: `ws-${t.id}`,
      label: `Go to ${t.label}`,
      group: "Workspace",
      action: () => {
        onNavWorkspace(currentProjectId, t.id);
        onClose();
      },
    });
  }

  for (const t of ADMIN_TABS) {
    if (!canSeeAdminTab(t.id, can)) continue;
    commands.push({
      id: `admin-${t.id}`,
      label: `Admin: ${t.label}`,
      group: "Admin",
      action: () => {
        onNavAdmin(t.id);
        onClose();
      },
    });
  }

  const filtered = query
    ? commands.filter((c) => c.label.toLowerCase().includes(query.toLowerCase()))
    : commands;

  const clampedCursor = filtered.length === 0 ? 0 : Math.min(cursor, filtered.length - 1);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  useEffect(() => {
    setCursor(0);
  }, [query]);

  function onKeyDown(e) {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => (c + 1) % Math.max(filtered.length, 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => (c - 1 + Math.max(filtered.length, 1)) % Math.max(filtered.length, 1));
    } else if (e.key === "Enter") {
      e.preventDefault();
      filtered[clampedCursor]?.action();
    } else if (e.key === "Escape") {
      onClose();
    }
  }

  if (!open) return null;

  return (
    <div className="cmd-overlay" onClick={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="cmd-dialog"
        onClick={(e) => e.stopPropagation()}
        onKeyDown={(e) => {
          if (e.key === "Tab") e.preventDefault();
        }}
      >
        <input
          ref={inputRef}
          className="cmd-input"
          placeholder="Type a command or search…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={onKeyDown}
          aria-autocomplete="list"
          aria-controls="cmd-listbox"
          aria-activedescendant={
            filtered[clampedCursor] ? `cmd-item-${filtered[clampedCursor].id}` : undefined
          }
        />
        {filtered.length === 0 ? (
          <div className="cmd-empty">No results for &ldquo;{query}&rdquo;</div>
        ) : (
          <ul id="cmd-listbox" role="listbox" className="cmd-list">
            {filtered.map((cmd, i) => (
              <li
                key={cmd.id}
                id={`cmd-item-${cmd.id}`}
                role="option"
                aria-selected={i === clampedCursor}
                className={`cmd-item${i === clampedCursor ? " cmd-item-active" : ""}`}
                onClick={cmd.action}
                onMouseEnter={() => setCursor(i)}
              >
                <span className="cmd-item-label">
                  <Highlighted text={cmd.label} query={query} />
                </span>
                <span className="cmd-item-group">{cmd.group}</span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}
