import { useEffect, useRef, useState } from "react";

// input_summary from the backend has a tool-name prefix, e.g. "Read foo.py", "Bash pytest"
function strip(summary, prefix) {
  return summary.startsWith(prefix + " ") ? summary.slice(prefix.length + 1) : summary;
}

function formatEvent(ev) {
  const tool = ev.tool || ev.name || "";
  const summary = ev.input_summary || "";

  if (tool === "Read" || tool === "read_file") {
    return `Reading ${strip(summary, "Read")}`;
  }
  if (tool === "Bash" || tool === "run_command") {
    let cmd = strip(summary, "Bash");
    if (cmd.length > 60) cmd = cmd.slice(0, 57) + "…";
    return `Running ${cmd}`;
  }
  if (tool === "Edit") {
    return `Editing ${strip(summary, "Edit")}`;
  }
  if (tool === "Write") {
    return `Editing ${strip(summary, "Write")}`;
  }
  if (tool === "Grep" || tool === "search") {
    return `Grep ${strip(summary, "Grep")}`;
  }
  if (tool === "Glob") {
    return `Glob ${strip(summary, "Glob")}`;
  }
  return tool || "Tool call";
}

export function ActivityFeed({ events, collapsible = true }) {
  const [open, setOpen] = useState(true);
  const bottomRef = useRef(null);

  useEffect(() => {
    if (open) bottomRef.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
  }, [events, open]);

  if (!events || events.length === 0) return null;

  return (
    <div className="activity-feed">
      {collapsible && (
        <div className="activity-feed-header" onClick={() => setOpen((o) => !o)}>
          <span>Activity ({events.length})</span>
          <span className="expand">{open ? "▾" : "▸"}</span>
        </div>
      )}
      {open && (
        <ul className="activity-feed-list">
          {events.slice(-12).map((ev, i) => (
            <li key={i} className="activity-event">
              {formatEvent(ev)}
            </li>
          ))}
          <li ref={bottomRef} />
        </ul>
      )}
    </div>
  );
}
