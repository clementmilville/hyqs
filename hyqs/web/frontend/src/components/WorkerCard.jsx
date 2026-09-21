import { useEffect, useState } from "react";
import { Crown } from "lucide-react";
import { STAGE_EMOJI, STAGE_LABEL } from "../constants.js";
import { elapsed } from "../utils.js";

const PROVIDER_ICON = { claude: "🟣", codex: "🟢" };

export function WorkerCard({ w, baseNow }) {
  const [, force] = useState(0);
  useEffect(() => {
    if (w.status !== "busy" || !w.alive) return;
    const id = setInterval(() => force((n) => n + 1), 1000);
    return () => clearInterval(id);
  }, [w.status, w.alive]);
  const drift = (Date.now() - baseNow) / 1000;
  const busyFor = w.busy_for + drift;
  const state = !w.alive ? "dead" : w.status === "busy" ? "busy" : "idle";
  const isSupervisor = w.role === "supervisor";
  return (
    <div className={`worker ${state}`}>
      <div className="worker-top">
        <span className={`worker-dot ${state}`} />
        <span className="worker-id" title={w.id}>
          {w.id}
        </span>
        {!w.alive && <span className="worker-tag dead">offline</span>}
        {w.alive && isSupervisor && (
          <span className="worker-tag">
            {w.status === "leader" ? (
              <>
                <Crown size={12} aria-hidden="true" /> leader
              </>
            ) : (
              <>
                <span className="worker-standby-dot" aria-hidden="true" /> standby
              </>
            )}
          </span>
        )}
        {w.alive && !isSupervisor && w.status === "idle" && (
          <span className="worker-tag idle">idle</span>
        )}
      </div>
      {state === "busy" ? (
        <div className="worker-job">
          <div className="worker-act">
            <span className="worker-step">
              {STAGE_EMOJI[w.stage] || "⚙️"} {STAGE_LABEL[w.stage] || w.stage}
            </span>
            {w.provider && (
              <span className="worker-prov">
                {PROVIDER_ICON[w.provider] || "•"} {w.provider}
              </span>
            )}
          </div>
          <div className="worker-idea">
            #{w.job_id} {w.idea}
          </div>
          <div className="worker-time">⏱ {elapsed(busyFor)}</div>
        </div>
      ) : (
        <div className="worker-job empty">
          {w.alive
            ? isSupervisor
              ? w.status
              : "waiting for work…"
            : `last seen ${elapsed(w.last_seen_ago + drift)} ago`}
        </div>
      )}
    </div>
  );
}
