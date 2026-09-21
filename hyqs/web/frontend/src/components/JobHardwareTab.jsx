import { useEffect, useState } from "react";
import { getJobResources } from "../api.js";

function fmtBytes(n) {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / (1024 * 1024)).toFixed(1)} MiB`;
  return `${(n / (1024 * 1024 * 1024)).toFixed(2)} GiB`;
}

function NetCell({ bytes, approx }) {
  if (bytes == null) return <td>—</td>;
  return (
    <td title={approx ? "host-level approximation" : undefined}>
      {approx ? "~" : ""}
      {fmtBytes(bytes)}
    </td>
  );
}

export function JobHardwareTab({ job }) {
  const [rows, setRows] = useState(null);

  useEffect(() => {
    let live = true;
    const load = () =>
      getJobResources(job.id)
        .then((d) => live && setRows(d))
        .catch(() => {});
    load();
    const active = job.status === "running" || job.status === "pending";
    const id = active ? setInterval(load, 5000) : null;
    return () => {
      live = false;
      if (id) clearInterval(id);
    };
  }, [job.id, job.status]);

  if (rows === null) return <div className="tab-empty">Loading…</div>;
  if (rows.length === 0) return <div className="tab-empty">No hardware data yet.</div>;

  return (
    <div className="hw-table-wrap">
      <table className="hw-table">
        <thead>
          <tr>
            <th>Stage</th>
            <th>CPU (s)</th>
            <th>RAM peak</th>
            <th>Net</th>
            <th>I/O read</th>
            <th>I/O write</th>
            <th>Wall (s)</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{r.stage}</td>
              <td>{r.cpu_seconds != null ? r.cpu_seconds.toFixed(2) : "—"}</td>
              <td>{fmtBytes(r.peak_rss_bytes)}</td>
              <NetCell bytes={r.net_bytes} approx={r.net_bytes_approx} />
              <td>{fmtBytes(r.io_read_bytes)}</td>
              <td>{fmtBytes(r.io_write_bytes)}</td>
              <td>{r.wall_seconds.toFixed(2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
