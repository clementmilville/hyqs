import { useEffect, useState } from "react";
import { getProjectLogs } from "../api.js";
import { PageState } from "./PageState.jsx";

// On-demand log tail for a deployed project (job #1109) — lets an admin read
// recent container/compose-service logs from the Ops Command Center without
// SSH. Manual refresh only for v1; live streaming can follow later.
//
// Props:
//   projectId:   number
//   projectName: string
//   services:    string[] | null — compose service names, or null for a
//                single-container project (hides the service selector)
//   onClose:     () => void
export function LogsDrawer({ projectId, projectName, services, onClose }) {
  const isCompose = Array.isArray(services) && services.length > 0;
  const [service, setService] = useState(isCompose ? services[0] : null);
  const [lines, setLines] = useState([]);
  const [truncated, setTruncated] = useState(false);
  const [loading, setLoading] = useState(true);
  const [errMsg, setErrMsg] = useState(null);

  async function fetchLogs(svc) {
    setLoading(true);
    setErrMsg(null);
    try {
      const data = await getProjectLogs(projectId, { service: svc || undefined });
      setLines(data.lines || []);
      setTruncated(!!data.truncated);
    } catch (e) {
      setErrMsg(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    fetchLogs(service);
  }, [projectId, service]);

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div
        className="modal-dialog logs-drawer-dialog"
        role="dialog"
        aria-modal="true"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-header">
          <span className="modal-title">Logs — {projectName}</span>
          <button className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </div>
        <div className="modal-body logs-drawer-body">
          <div className="logs-drawer-toolbar">
            {isCompose && (
              <select
                className="logs-service-select"
                value={service}
                onChange={(e) => setService(e.target.value)}
              >
                {services.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
            )}
            <button
              className="btn-secondary tap-target"
              disabled={loading}
              onClick={() => fetchLogs(service)}
            >
              {loading ? "Refreshing…" : "Refresh"}
            </button>
          </div>
          <PageState
            forbidden={errMsg === "forbidden"}
            error={!!errMsg && errMsg !== "forbidden"}
            loading={loading}
            retry={() => fetchLogs(service)}
          >
            {lines.length === 0 ? (
              <p className="hint">No log lines available.</p>
            ) : (
              <pre className="logs-drawer-viewer">{lines.join("\n")}</pre>
            )}
            {truncated && <p className="hint">Showing the most recent lines only.</p>}
          </PageState>
        </div>
      </div>
    </div>
  );
}
