import { useState, useEffect } from "react";
import { getProjectChangelog, getDecision } from "../api.js";
import { MarkdownContent } from "../components/MarkdownContent.jsx";
import { FeedList, groupByDay } from "../components/FeedList.jsx";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "../components/PageState.jsx";

const DAYS_PER_PAGE = 3;

const TRIGGER_LABEL = {
  pipeline_job: "Pipeline",
  manual: "Manual",
  auto_poller: "Auto",
};

const TRIGGER_CLASS = {
  pipeline_job: "badge run",
  manual: "badge",
  auto_poller: "badge ok",
};

// The default/majority trigger ("pipeline_job") is noise when it repeats on
// every row — only show the badge when it differs from that default.
function TriggerBadge({ trigger }) {
  if (trigger === "pipeline_job") return null;
  return (
    <span className={TRIGGER_CLASS[trigger] ?? "badge"}>{TRIGGER_LABEL[trigger] ?? "Unknown"}</span>
  );
}

function JobDecisionToggle({ projectId, filename }) {
  const [expanded, setExpanded] = useState(false);
  const [content, setContent] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  async function handleToggle() {
    const opening = !expanded;
    setExpanded(opening);
    if (!opening || content !== null || loading) return;
    setLoading(true);
    try {
      const resp = await getDecision(projectId, filename);
      setContent(resp.content);
    } catch (e) {
      setError(`Couldn't load the decision: ${e.message}`);
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="changelog-decision">
      <button
        className="changelog-decision-toggle tap-target"
        onClick={handleToggle}
        aria-expanded={expanded}
      >
        <span>Why</span>
        <span className="changelog-expand-icon" aria-hidden="true">
          {expanded ? "▲" : "▼"}
        </span>
      </button>
      {expanded && (
        <div className="changelog-decision-body">
          {loading ? (
            <p className="hint">Loading…</p>
          ) : error ? (
            <p className="hint">{error}</p>
          ) : (
            <MarkdownContent content={content} />
          )}
        </div>
      )}
    </div>
  );
}

function JobChange({ change, projectId }) {
  return (
    <div className="changelog-change changelog-change-job">
      <span className="changelog-job-title">{change.title}</span>
      {change.summary && (
        <div className="changelog-job-summary">
          <MarkdownContent content={change.summary} />
        </div>
      )}
      {change.decision_filename && (
        <JobDecisionToggle projectId={projectId} filename={change.decision_filename} />
      )}
    </div>
  );
}

function CommitChange({ change }) {
  return (
    <div className="changelog-change changelog-change-commit">
      <span className="changelog-commit-meta">
        {change.author} · {change.subject}{" "}
        <span className="changelog-sha">{change.sha?.slice(0, 7)}</span>
      </span>
      {change.prose && <div className="changelog-prose muted">{change.prose}</div>}
    </div>
  );
}

function ReleaseRow({ release, showDate = true, projectId }) {
  const [expanded, setExpanded] = useState(false);
  const shortSha = release.deployed_commit?.slice(0, 7) ?? "—";
  const time = release.deployed_at
    ? new Date(release.deployed_at).toLocaleTimeString("en-GB", {
        hour: "2-digit",
        minute: "2-digit",
      })
    : "—";
  const date =
    showDate && release.deployed_at
      ? `${new Date(release.deployed_at).toLocaleDateString("en-GB", {
          day: "numeric",
          month: "short",
          year: "numeric",
        })} · ${time}`
      : time;
  const showChangeCount = release.changes.length > 0 && !release.initial_deploy;

  return (
    <div className="changelog-release">
      <div className="table-scroll">
        <button
          className="changelog-release-header tap-target"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
        >
          <span className="changelog-release-date">{date}</span>
          <span className="changelog-release-sha">{shortSha}</span>
          <TriggerBadge trigger={release.trigger} />
          {release.verified && <span className="badge ok">✓ verified</span>}
          {release.initial_deploy && (
            <span className="changelog-initial-label">Initial deploy</span>
          )}
          {showChangeCount && (
            <span className="changelog-change-count">
              {release.changes.length} change{release.changes.length !== 1 ? "s" : ""}
            </span>
          )}
          <span className="changelog-expand-icon" aria-hidden="true">
            {expanded ? "▲" : "▼"}
          </span>
        </button>
      </div>
      {expanded && (
        <div className="changelog-release-body">
          {release.initial_deploy ? (
            <p className="hint">First deploy — no prior release to compare against.</p>
          ) : release.changes.length === 0 ? (
            <p className="hint">No changes recorded for this release.</p>
          ) : (
            release.changes.map((c, i) =>
              c.kind === "job" ? (
                <JobChange key={i} change={c} projectId={projectId} />
              ) : (
                <CommitChange key={i} change={c} />
              )
            )
          )}
        </div>
      )}
    </div>
  );
}

// Kept for callers/tests that expect the old { day, releases } shape — the
// actual bucketing now lives in FeedList.jsx's generic groupByDay.
export function groupReleasesByDay(releases) {
  return groupByDay(releases, (r) => r.deployed_at).map((g) => ({
    day: g.day,
    releases: g.items,
  }));
}

export function ChangelogTab({ projectId }) {
  const { data, loading, forbidden, error, retry } = usePageData(
    () => getProjectChangelog(projectId),
    [projectId]
  );
  const [visibleDayCount, setVisibleDayCount] = useState(DAYS_PER_PAGE);

  useEffect(() => {
    setVisibleDayCount(DAYS_PER_PAGE);
  }, [projectId]);

  const releases = data?.releases ?? [];
  const dayGroups = groupByDay(releases, (r) => r.deployed_at);
  const visibleReleases = dayGroups.slice(0, visibleDayCount).flatMap((g) => g.items);
  const hasMore = dayGroups.length > visibleDayCount;

  return (
    <div className="changelog-tab">
      <h2>Changelog</h2>
      <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
        {releases.length === 0 ? (
          <p className="hint">No releases yet.</p>
        ) : (
          <>
            <FeedList
              items={visibleReleases}
              getDate={(r) => r.deployed_at}
              renderRow={(r, i) => (
                <ReleaseRow key={i} release={r} showDate={false} projectId={projectId} />
              )}
              dayGroupClassName="changelog-day-group"
              dayHeaderClassName="changelog-day-header"
            />
            {hasMore && (
              <button
                className="btn-secondary tap-target"
                onClick={() => setVisibleDayCount((n) => n + DAYS_PER_PAGE)}
              >
                Show earlier
              </button>
            )}
          </>
        )}
      </PageState>
    </div>
  );
}
