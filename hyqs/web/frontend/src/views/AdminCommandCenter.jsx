import { useEffect, useRef, useState } from "react";
import { useFleetData } from "../hooks/useFleetData.js";
import { WorkerCard } from "../components/WorkerCard.jsx";
import { EmptyState } from "../components/EmptyState.jsx";
import { PageState } from "../components/PageState.jsx";
import { FeedList } from "../components/FeedList.jsx";
import { AdminRuntime } from "./AdminRuntime.jsx";
import { COMMAND_CENTER_TABS, COMMAND_CENTER_TAB_LABEL } from "../constants.js";

const ACTION_ICON = {
  requeued: "↻",
  dead_lettered: "✗",
  escalated: "⚠",
  reconciled: "✓",
};

const DAY_MS = 24 * 60 * 60 * 1000;

// e.detail is a plain TEXT column: for AI-diagnosed actions it's a JSON blob
// ({diagnosis, action, confidence, valid}); for everything else it's already
// a short plain string. Fall back to the raw string when it doesn't parse.
function RemediationDetail({ detail }) {
  if (!detail) return null;
  let parsed = null;
  try {
    parsed = JSON.parse(detail);
  } catch {
    return <span>{detail}</span>;
  }
  const summary = (parsed.diagnosis || detail).split("\n")[0];
  return (
    <details>
      <summary>{summary}</summary>
      <pre className="sup-detail-full">{JSON.stringify(parsed, null, 2)}</pre>
    </details>
  );
}

// remediation_feed arrives newest-first: merge consecutive entries sharing
// the same (job_id, failure_class) into one row, keeping the first (most
// recent) entry's ts/action/detail as representative and counting the rest.
function coalesceRemediationFeed(events) {
  const result = [];
  for (const e of events) {
    const last = result[result.length - 1];
    if (last && last.job_id === e.job_id && last.failure_class === e.failure_class) {
      last.count += 1;
    } else {
      result.push({ ...e, count: 1, latestTs: e.ts });
    }
  }
  return result;
}

function RemediationRow({ entry }) {
  return (
    <div className="sup-feed-row">
      <span className="sup-ts">{new Date(entry.latestTs * 1000).toLocaleString()}</span>
      <span className="sup-action-icon">{ACTION_ICON[entry.action] ?? "•"}</span>
      <span className="sup-feed-job">#{entry.job_id}</span>
      <span className="badge">{entry.failure_class}</span>
      {entry.count > 1 && <span className="sup-count-chip">×{entry.count}</span>}
      <span className="sup-detail">
        <RemediationDetail detail={entry.detail} />
      </span>
    </div>
  );
}

// Coalesces the raw remediation_feed into count-chip'd rows, shows only the
// last 24h by default (CONVENTIONS.md §9.3 attention-first, not a wall of
// history), and reveals older entries behind a "Show earlier" toggle that
// auto-expands only when there's nothing recent to show (mirrors
// WorkLanes.jsx's autoExpandRecentlyFinished pattern).
function RemediationFeed({ events, nowMs }) {
  const coalesced = coalesceRemediationFeed(events);
  const recent = coalesced.filter((e) => nowMs - e.latestTs * 1000 <= DAY_MS);
  const earlier = coalesced.filter((e) => nowMs - e.latestTs * 1000 > DAY_MS);

  const [earlierOpen, setEarlierOpen] = useState(recent.length === 0 && earlier.length > 0);
  const userToggledRef = useRef(false);

  useEffect(() => {
    if (!userToggledRef.current) setEarlierOpen(recent.length === 0 && earlier.length > 0);
  }, [recent.length, earlier.length]);

  if (coalesced.length === 0) {
    return <p className="hint">No remediation events recorded.</p>;
  }

  return (
    <>
      <div className="table-scroll">
        <FeedList
          items={recent}
          getDate={(e) => e.latestTs * 1000}
          renderRow={(e) => <RemediationRow key={e.id} entry={e} />}
        />
      </div>
      {earlier.length > 0 && (
        <>
          <button
            type="button"
            className="btn-secondary tap-target sup-feed-earlier-toggle"
            onClick={() => {
              userToggledRef.current = true;
              setEarlierOpen((v) => !v);
            }}
          >
            {earlierOpen ? "Hide earlier" : `Show earlier (${earlier.length})`}
          </button>
          {earlierOpen && (
            <div className="table-scroll">
              <FeedList
                items={earlier}
                getDate={(e) => e.latestTs * 1000}
                renderRow={(e) => <RemediationRow key={e.id} entry={e} />}
              />
            </div>
          )}
        </>
      )}
    </>
  );
}

function LaneHeader({ title, count }) {
  return (
    <div className={`work-lane-header${count === 0 ? " work-lane-header-empty" : ""}`}>
      <span className="work-lane-title">{title}</span>
      <span className="badge work-lane-count">{count}</span>
    </div>
  );
}

function PipelineTab({ fleetData, supData, baseNow }) {
  const { workers = [], pauses = [], stats = {} } = fleetData ?? {};
  const {
    supervisors = [],
    needs_judgment = [],
    dead_lettered = [],
    remediation_feed = [],
  } = supData ?? {};

  const leaderCount = supervisors.filter((s) => s.status === "leader").length;
  const standbyCount = supervisors.filter((s) => s.status === "standby").length;
  const nowMs = baseNow ?? Date.now();

  return (
    <div className="work-lanes">
      <section className="work-lane">
        <LaneHeader title="Needs Attention" count={needs_judgment.length + dead_lettered.length} />
        {needs_judgment.length === 0 && dead_lettered.length === 0 ? (
          <p className="hint">No failed jobs requiring attention.</p>
        ) : (
          <>
            {needs_judgment.length > 0 && (
              <div className="table-scroll">
                <table className="sup-table">
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>Idea</th>
                      <th>Stage</th>
                      <th>Class</th>
                      <th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {needs_judgment.map((j) => (
                      <tr key={j.job_id}>
                        <td>#{j.job_id}</td>
                        <td className="sup-idea" title={j.idea}>
                          {j.idea}
                        </td>
                        <td>{j.stage}</td>
                        <td>
                          <span className="badge">{j.failure_class}</span>
                        </td>
                        <td>{j.is_supervisor_notified && "🔔"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
            {dead_lettered.length > 0 && (
              <div className="table-scroll">
                <table className="sup-table">
                  <thead>
                    <tr>
                      <th>Job</th>
                      <th>Idea</th>
                      <th>Stage</th>
                      <th>Class</th>
                      <th>Requeues</th>
                    </tr>
                  </thead>
                  <tbody>
                    {dead_lettered.map((j) => (
                      <tr key={j.job_id}>
                        <td>#{j.job_id}</td>
                        <td className="sup-idea" title={j.idea}>
                          {j.idea}
                        </td>
                        <td>{j.stage}</td>
                        <td>
                          <span className="badge bad">{j.failure_class}</span>
                        </td>
                        <td>{j.supervisor_requeue_count}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </>
        )}
      </section>

      <section className="work-lane">
        <LaneHeader title="Running Now" count={stats.running ?? 0} />
        <div className="fleet-stats stat-grid">
          <div>
            <span>{stats.alive ?? 0}</span>workers online
          </div>
          <div>
            <span>{stats.busy ?? 0}</span>building now
          </div>
          <div>
            <span>{leaderCount}</span>leader
          </div>
          <div>
            <span>{standbyCount}</span>standby
          </div>
        </div>
        {pauses.length > 0 && (
          <div className="fleet-banner warn">
            ⏸️ Paused:{" "}
            {pauses
              .map(
                (p) => `${p.provider} (resumes ${new Date(p.until * 1000).toLocaleTimeString()})`
              )
              .join(", ")}
          </div>
        )}
        <div className="workers">
          {workers.length === 0 ? (
            <EmptyState icon="🤖" title="No workers running" hint="Start one with hyqs-pipeline." />
          ) : (
            workers.map((w) => <WorkerCard key={w.id} w={w} baseNow={baseNow} />)
          )}
        </div>
      </section>

      <section className="work-lane">
        <LaneHeader title="Queued" count={stats.pending ?? 0} />
        <div className="fleet-stats stat-grid">
          <div>
            <span>{stats.pending ?? 0}</span>jobs queued
          </div>
        </div>
      </section>

      <section className="work-lane">
        <LaneHeader title="Recently Finished" count={remediation_feed.length} />
        <RemediationFeed events={remediation_feed} nowMs={nowMs} />
      </section>
    </div>
  );
}

function DeploymentsTab({ mergeLocks }) {
  if (mergeLocks.length === 0) {
    return (
      <EmptyState
        icon="🔒"
        title="No active merges"
        hint="A project appears here while its merge stage holds the repo's git lock."
      />
    );
  }
  return (
    <div className="table-scroll">
      <table className="sup-table">
        <thead>
          <tr>
            <th>Project</th>
            <th>Owner</th>
          </tr>
        </thead>
        <tbody>
          {mergeLocks.map((m) => (
            <tr key={m.repo_path}>
              <td>{m.repo_path.split("/").pop()}</td>
              <td>{m.owner}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

// Platform-wide execution health: the same attention-first lane structure as
// the project Work page (see CONVENTIONS.md section 9, rule 6), but one
// altitude up — replaces the old separate Overview + Fleet admin pages.
// Sub-tabbed into Pipeline (attention lanes), Deployments (merge locks), and
// Runtime (fleet-wide container/health/drift, jobs #1105/#1106).
export function AdminCommandCenter({ subTab, onNavTab }) {
  const { fleetData, supData, forbidden, error, retry, lastFleetAt: baseNow } = useFleetData();
  const activeTab = COMMAND_CENTER_TABS.includes(subTab) ? subTab : "pipeline";
  const { merge_locks = [] } = fleetData ?? {};

  return (
    <div className="admin-command-center">
      <div className="job-tabs plan-sub-tabs">
        <div className="job-tab-bar">
          {COMMAND_CENTER_TABS.map((t) => (
            <button
              key={t}
              className={`job-tab-btn tap-target${activeTab === t ? " active" : ""}`}
              onClick={() => onNavTab?.(t)}
            >
              {COMMAND_CENTER_TAB_LABEL[t]}
            </button>
          ))}
        </div>
      </div>

      {activeTab === "runtime" ? (
        <AdminRuntime />
      ) : (
        <PageState
          forbidden={forbidden}
          error={error}
          loading={!fleetData || !supData}
          retry={retry}
        >
          {activeTab === "pipeline" ? (
            <PipelineTab fleetData={fleetData} supData={supData} baseNow={baseNow} />
          ) : (
            <DeploymentsTab mergeLocks={merge_locks} />
          )}
        </PageState>
      )}
    </div>
  );
}
