import { useMemo, useState } from "react";
import { STAGE_EMOJI, STAGE_LABEL, STATUS_CLASS } from "../constants.js";
import { dur } from "../utils.js";
import { EventDetail } from "./EventDetail.jsx";

function executorAttribution(event) {
  const name = event.agent_name?.trim();
  if (!name) return null;

  const metadata = [event.agent_provider, event.agent_model]
    .map((value) => value?.trim())
    .filter(Boolean);
  return [name, ...metadata].join(" · ");
}

export function JobTimeline({ job, events = [], autoExpandFailed = false }) {
  const initialOpen = useMemo(() => {
    if (!autoExpandFailed) return {};
    const failed = [...events].reverse().find((e) => e.status === "failed");
    return failed ? { [failed.id]: true } : {};
  }, []);

  const [openRows, setOpenRows] = useState(initialOpen);

  const toggle = (id) =>
    setOpenRows((prev) => ({ ...prev, [id]: !prev[id] }));

  if (events.length === 0)
    return (
      <p className="hint timeline-empty">
        No step activity recorded yet — this job ran before the timeline existed, or hasn't started.
      </p>
    );

  return (
    <div className="timeline">
      {events.map((ev) => {
        const attribution = executorAttribution(ev);
        return (
          <div key={ev.id} className={`tl-row ${STATUS_CLASS[ev.status] || ""}`}>
            <span className="tl-dot">
              {ev.status === "failed" ? "✗" : ev.status === "done" ? "✓" : "●"}
            </span>
            <div className="tl-body">
              <div className="tl-head clickable" onClick={() => toggle(ev.id)}>
                <span className="tl-row-expand">{openRows[ev.id] ? "▾" : "▸"}</span>
                <span className="tl-stage">
                  {STAGE_EMOJI[ev.stage]} {STAGE_LABEL[ev.stage] || ev.stage}
                </span>
                {attribution && <span className="hint">{attribution}</span>}
                {ev.attempt > 0 && <span className="tl-attempt">attempt {ev.attempt}</span>}
                <span className="tl-meta">
                  {dur(ev.started_at, ev.ended_at) && (
                    <span>⏱ {dur(ev.started_at, ev.ended_at)}</span>
                  )}
                  {ev.tokens > 0 && <span> · {ev.tokens.toLocaleString()} tok</span>}
                  {ev.cost_usd > 0 && <span> · ${ev.cost_usd.toFixed(4)}</span>}
                </span>
              </div>
              {openRows[ev.id] && (
                <EventDetail ev={ev} jobId={job.id} onViewDiff={() => {}} />
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
