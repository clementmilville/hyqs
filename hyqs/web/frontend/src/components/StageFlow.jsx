import React from "react";
import { STEPS, STAGE_DONE, STAGE_EMOJI } from "../constants.js";

export function StageFlow({ job }) {
  const done = STAGE_DONE[job.stage] ?? 0;
  const allDone = job.status === "done";
  const failed = job.status === "failed";
  return (
    <div className="flow">
      {STEPS.map((s, i) => {
        let state;
        if (allDone || i < done) state = "done";
        else if (i === done) state = failed ? "failed" : "active";
        else state = "pending";
        const icon = state === "done" ? "✓" : state === "failed" ? "✗" : STAGE_EMOJI[s];
        return (
          <React.Fragment key={s}>
            {i > 0 && <span className={`flow-line ${i <= done && !failed ? "on" : ""}`} />}
            <span className={`node ${state}`} title={s}>
              <span className="dot">{icon}</span>
              <span className="nlabel">{s}</span>
            </span>
          </React.Fragment>
        );
      })}
      {job.attempts > 0 && (
        <span className="fixbadge" title={`${job.attempts} self-heal round(s)`}>
          🔁 ×{job.attempts}
          {job.stage === "fix" ? " fixing…" : ""}
        </span>
      )}
    </div>
  );
}
