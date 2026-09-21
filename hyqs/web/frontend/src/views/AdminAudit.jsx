import { Fragment, useEffect, useState } from "react";
import { getAuditLog } from "../api.js";
import { PageState } from "../components/PageState.jsx";
import { usePageData } from "../hooks/usePageData.js";
import { groupByDay, formatDayHeader } from "../components/FeedList.jsx";

const TABLES = [
  "",
  "jobs",
  "projects",
  "epics",
  "project_agents",
  "project_members",
  "users",
  "invitations",
  "backlog_items",
  "chat_sessions",
  "deploys",
  "meta",
  "role_permissions",
];

const ACTION_CLASS = { insert: "ok", update: "run", delete: "bad" };
const PAGE_LIMIT = 100;

function fmtChanged(changed) {
  if (!changed || typeof changed !== "object") return "";
  return Object.entries(changed)
    .map(([k, v]) => (Array.isArray(v) ? `${k}: ${v[0]} → ${v[1]}` : k))
    .join("; ");
}

function sameMinute(a, b) {
  if (!a || !b) return false;
  const da = new Date(a);
  const db = new Date(b);
  if (Number.isNaN(da.getTime()) || Number.isNaN(db.getTime())) return false;
  return Math.floor(da.getTime() / 60000) === Math.floor(db.getTime() / 60000);
}

// Merge consecutive same-actor+same-table+same-action entries that land in
// the same UTC minute into one burst row — keeps a boot-time flood of
// identical role_permissions inserts (or any repeated bulk op) from
// rendering as dozens of individually-scanned rows.
export function coalesceAuditBursts(entries) {
  const bursts = [];
  for (const e of entries) {
    const last = bursts[bursts.length - 1];
    const sameBurst =
      last &&
      last.actor === e.actor &&
      last.table_name === e.table_name &&
      last.action === e.action &&
      sameMinute(last.at, e.at);
    if (sameBurst) {
      last.count += 1;
      last.rowPks.push(e.row_pk);
      last.changedList.push(e.changed);
    } else {
      bursts.push({
        id: e.id,
        at: e.at,
        actor: e.actor,
        table_name: e.table_name,
        action: e.action,
        count: 1,
        rowPks: [e.row_pk],
        changedList: [e.changed],
      });
    }
  }
  return bursts;
}

function ChangedCell({ changedList, rowPks }) {
  const [expanded, setExpanded] = useState(false);
  const nonEmpty = changedList
    .map((c, i) => ({ c, rowPk: rowPks[i] }))
    .filter(({ c }) => c && typeof c === "object" && Object.keys(c).length > 0);
  const fieldCount = nonEmpty.reduce((sum, { c }) => sum + Object.keys(c).length, 0);

  if (fieldCount === 0) return <span style={{ color: "var(--dim)" }}>—</span>;

  return (
    <div>
      <button
        className="link tap-target"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
      >
        {fieldCount} field{fieldCount !== 1 ? "s" : ""}
      </button>
      {expanded && (
        <div className="audit-changed-detail">
          {nonEmpty.map(({ c, rowPk }, i) => (
            <div key={i}>
              {nonEmpty.length > 1 && <span className="audit-changed-row">{rowPk}: </span>}
              {fmtChanged(c)}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function AuditRow({ row }) {
  return (
    <tr>
      <td className="audit-when">{row.at ? new Date(row.at).toLocaleString() : "—"}</td>
      <td className="audit-actor">{row.actor}</td>
      <td>{row.table_name}</td>
      <td style={{ color: "var(--dim)" }}>{row.count > 1 ? `${row.count} rows` : row.rowPks[0]}</td>
      <td>
        <span className={`badge ${ACTION_CLASS[row.action] || ""}`}>{row.action}</span>
        {row.count > 1 && <span className="badge audit-burst-chip">×{row.count}</span>}
      </td>
      <td>
        <ChangedCell changedList={row.changedList} rowPks={row.rowPks} />
      </td>
    </tr>
  );
}

export function AdminAudit() {
  const [table, setTable] = useState("");
  const [actor, setActor] = useState("");
  const [debounced, setDebounced] = useState({ table: "", actor: "" });
  const [entries, setEntries] = useState([]);
  const [olderError, setOlderError] = useState("");

  useEffect(() => {
    const t = setTimeout(() => setDebounced({ table, actor }), 250);
    return () => clearTimeout(t);
  }, [table, actor]);

  const { data, loading, forbidden, error, retry } = usePageData(
    () => getAuditLog({ table: debounced.table, actor: debounced.actor, limit: PAGE_LIMIT }),
    [debounced.table, debounced.actor]
  );

  useEffect(() => {
    setEntries(data || []);
    setOlderError("");
  }, [data]);

  function loadMore() {
    if (entries.length === 0) return;
    const last = entries[entries.length - 1].id;
    getAuditLog({
      table: debounced.table,
      actor: debounced.actor,
      limit: PAGE_LIMIT,
      before_id: last,
    })
      .then((more) => setEntries((cur) => [...cur, ...more]))
      .catch((e) => setOlderError(e.message || "Failed to load older entries."));
  }

  const groups = groupByDay(coalesceAuditBursts(entries), (r) => r.at);

  return (
    <div>
      <h2>Audit Trail</h2>
      <p className="hint">
        Every change to jobs, projects, agents, users, permissions and settings — who did it and
        when. Rows older than 90 days are pruned.
      </p>

      <div className="perf-filter-bar">
        <label className="perf-filter-field">
          <span className="perf-filter-label">Table</span>
          <select value={table} onChange={(e) => setTable(e.target.value)}>
            {TABLES.map((t) => (
              <option key={t} value={t}>
                {t || "All tables"}
              </option>
            ))}
          </select>
        </label>
        <label className="perf-filter-field">
          <span className="perf-filter-label">Actor</span>
          <input
            type="text"
            placeholder="user:, worker:, supervisor:, db:"
            value={actor}
            onChange={(e) => setActor(e.target.value)}
          />
        </label>
      </div>

      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        {entries.length === 0 ? (
          <p className="hint">No audit entries match.</p>
        ) : (
          <>
            <div className="table-scroll" style={{ marginTop: "var(--space-4)" }}>
              <table className="perf-jobs-table">
                <thead>
                  <tr>
                    <th>When</th>
                    <th>Actor</th>
                    <th>Table</th>
                    <th>Row</th>
                    <th>Action</th>
                    <th>Changed</th>
                  </tr>
                </thead>
                <tbody>
                  {groups.map((group) => (
                    <Fragment key={group.day}>
                      <tr>
                        <td colSpan={6} className="feed-list-day-header">
                          {formatDayHeader(group.day)}
                        </td>
                      </tr>
                      {group.items.map((row) => (
                        <AuditRow key={row.id} row={row} />
                      ))}
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
            {olderError && <p className="hint">{olderError}</p>}
            <button
              className="btn-secondary tap-target"
              style={{ marginTop: "var(--space-3)" }}
              onClick={loadMore}
            >
              Load older
            </button>
          </>
        )}
      </PageState>
    </div>
  );
}
