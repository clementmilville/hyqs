import { Fragment, useEffect, useState } from "react";
import { STATUS_CLASS, PRIORITY_LEVELS } from "../constants.js";
import { ago } from "../utils.js";
import { EmptyState } from "./EmptyState.jsx";
import { ClipboardList } from "lucide-react";

export const STATUS_FILTER_CHIPS = ["active", "done", "failed", "archived", "all"];
export const STATUS_FILTER_LABEL = {
  active: "Active",
  done: "Done",
  failed: "Failed",
  archived: "Archived",
  all: "All",
};

// The stable vocabulary used anywhere a filing channel is presented. Keep the
// stored value separate from the label so filtering continues to use API values.
export const CHANNEL_LABEL = {
  ui: "Web UI",
  supervisor: "Supervisor",
  cli: "CLI",
  intake: "Intake",
  mcp: "MCP",
  slack: "Slack",
  api: "API",
  deploy: "Deploy",
  unknown: "Unknown",
};
export const SOURCE_LABEL = CHANNEL_LABEL;

export const SOURCE_COLOR = {
  ui: "source-ui",
  supervisor: "source-supervisor",
  cli: "source-cli",
  intake: "source-intake",
  mcp: "source-mcp",
  slack: "source-unknown",
  api: "source-unknown",
  deploy: "source-unknown",
  unknown: "source-unknown",
};

export const PRIORITY_LABEL = Object.fromEntries(
  Object.entries(PRIORITY_LEVELS).map(([key, value]) => [value, key])
);

// A human-readable tooltip summary of why a job's effective priority
// differs from its base priority (one line per boost reason).
export function describePriorityReasons(reasons) {
  if (!Array.isArray(reasons) || reasons.length === 0) return "Priority boosted";
  return reasons.map((r) => `+${r.amount} ${r.detail}`).join("\n");
}

export const WORKLIST_COLUMNS = [
  { key: "id", label: "#", width: "52px" },
  { key: "title", label: "Title", width: "1fr" },
  { key: "status", label: "Status", width: "88px" },
  { key: "stage", label: "Stage", width: "80px" },
  { key: "priority", label: "Priority", width: "80px" },
  { key: "epic", label: "Epic", width: "120px" },
  { key: "channel", label: "Channel", width: "100px" },
  { key: "actor", label: "Actor", width: "120px" },
  { key: "executor", label: "Executor", width: "120px" },
  { key: "dependencies", label: "Depends on", width: "120px" },
  { key: "created_at", label: "Created", width: "100px" },
  { key: "updated_at", label: "Updated", width: "100px" },
];

export const DEFAULT_DESKTOP_COLUMN_KEYS = [
  "id",
  "title",
  "status",
  "stage",
  "priority",
  "epic",
  "channel",
  "actor",
  "executor",
  "updated_at",
];
export const DEFAULT_DESKTOP_COLUMNS = DEFAULT_DESKTOP_COLUMN_KEYS.map((key) =>
  WORKLIST_COLUMNS.find((column) => column.key === key)
);
// Backwards-compatible preset used by JobTable and WorkLanes until those
// callers opt into the canonical renderer and its independently projected
// channel/actor/executor columns.
export const COLUMNS = [
  ...WORKLIST_COLUMNS.filter((column) =>
    ["id", "title", "status", "stage", "priority", "epic"].includes(column.key)
  ),
  { key: "source", label: "Source", width: "130px" },
  WORKLIST_COLUMNS.find((column) => column.key === "updated_at"),
];
export const MOBILE_WORKLIST_COLUMN_KEYS = [
  "id",
  "title",
  "status",
  "stage",
  "executor",
  "dependencies",
];

function clean(value) {
  return value == null ? "" : String(value).trim();
}

export function getChannel(job) {
  return clean(job?.source).toLowerCase() || "unknown";
}

export function getChannelLabel(job) {
  const channel = getChannel(job);
  return CHANNEL_LABEL[channel] ?? channel;
}

export function getActor(job) {
  return clean(job?.source_actor);
}

export function getActorLabel(job) {
  const actor = getActor(job);
  return actor.includes("@") ? actor.split("@")[0] : actor;
}

export function getExecutor(job) {
  const current = job?.current_executor;
  if (current && typeof current === "object") {
    return {
      label: clean(current.label) || clean(current.provider),
      provider: clean(current.provider),
    };
  }
  return {
    label: clean(job?.executor_label) || clean(job?.agent_name) || clean(job?.agent_provider),
    provider: clean(job?.executor_provider) || clean(job?.agent_provider),
  };
}

export function getStage(job) {
  return clean(job?.stage);
}

// Retained for JobCard compatibility. New worklist cells intentionally render
// actor in its own column rather than appending it to the channel badge.
export function getSourceInfo(job) {
  const sourceLabel = getChannel(job);
  const actorLabel = getActorLabel(job) || null;
  const title = [
    getActor(job) ? `Filed by ${getActor(job)}` : null,
    `via ${sourceLabel}`,
    job?.created_at ? `at ${new Date(job.created_at).toLocaleString()}` : null,
  ]
    .filter(Boolean)
    .join(" ");
  return { sourceLabel, channelLabel: getChannelLabel(job), actorLabel, title };
}

export const ATTENTION_RANK = {
  failed: 0,
  running: 1,
  deploying: 1,
  pending: 2,
  done: 3,
  cancelled: 4,
};

export function isUnverifiedDone(job) {
  return job.status === "done" && job.resolution === "already-satisfied";
}

export function getStatusBadge(job) {
  if (isUnverifiedDone(job)) {
    return {
      className: "badge warn",
      label: "unverified",
      title: "Marked done with no diff — the AI judged the work already satisfied",
    };
  }
  return {
    className: `badge ${STATUS_CLASS[job.status]}`,
    label: job.status,
    title: undefined,
  };
}

export function getAttentionRank(job) {
  if (job.archived) return 99;
  if (isUnverifiedDone(job)) return 2.5;
  return ATTENTION_RANK[job.status] ?? 50;
}

export function normalizeDependencies(job) {
  const raw = job?.dependency_chain ?? job?.waiting_on ?? job?.depends_on ?? [];
  const values = Array.isArray(raw) ? raw : [raw];
  return values
    .map((dependency) => {
      if (dependency && typeof dependency === "object") {
        const id = dependency.id ?? dependency.job_id;
        if (id == null) return null;
        return { id, title: clean(dependency.title) };
      }
      return dependency == null || dependency === "" ? null : { id: dependency, title: "" };
    })
    .filter(Boolean);
}

export function getJobValue(job, key, epicsById = {}) {
  switch (key) {
    case "id":
      return Number(job.id) || 0;
    case "title":
      return clean(job.title || job.idea).toLocaleLowerCase();
    case "status":
      return clean(job.status).toLocaleLowerCase();
    case "stage":
      return getStage(job).toLocaleLowerCase();
    case "priority":
      return Number(job.priority) || 0;
    case "epic":
      return clean(epicsById[job.epic_id]?.name).toLocaleLowerCase();
    case "source":
    case "channel":
      return getChannel(job);
    case "actor":
      return getActor(job).toLocaleLowerCase();
    case "executor":
    case "provider": {
      const executor = getExecutor(job);
      return clean(executor.label || executor.provider).toLocaleLowerCase();
    }
    case "dependencies":
      return normalizeDependencies(job)
        .map(({ id }) => String(id))
        .join(",");
    case "created_at":
    case "updated_at":
      return clean(job[key]);
    case "attention":
      return getAttentionRank(job);
    default:
      return clean(job[key]).toLocaleLowerCase();
  }
}

export function sortJobs(jobs, sortKeys = [], epicsById = {}) {
  if (!sortKeys.length) return [...jobs];
  return [...jobs].sort((a, b) => {
    for (const { key, dir } of sortKeys) {
      const av = getJobValue(a, key, epicsById);
      const bv = getJobValue(b, key, epicsById);
      const result =
        typeof av === "string" && typeof bv === "string"
          ? av.localeCompare(bv, undefined, { numeric: true, sensitivity: "base" })
          : av < bv
            ? -1
            : av > bv
              ? 1
              : 0;
      if (result) return dir === "desc" ? -result : result;
    }
    return 0;
  });
}

function matchesChoice(actualValues, requested) {
  if (requested == null || requested === "" || requested === "all") return true;
  const wanted = (Array.isArray(requested) ? requested : [requested]).map((value) =>
    clean(value).toLocaleLowerCase()
  );
  return actualValues.some((value) => wanted.includes(clean(value).toLocaleLowerCase()));
}

export function filterJobs(
  jobs,
  {
    search = "",
    searchQuery = "",
    epic = null,
    epicId = null,
    provider = null,
    executor = null,
    channel = null,
    source = null,
    actor = null,
  } = {},
  epicsById = {}
) {
  const query = clean(search || searchQuery).toLocaleLowerCase();
  const selectedEpic = epicId ?? epic;
  return jobs.filter((job) => {
    const currentExecutor = getExecutor(job);
    const searchable = [
      job.id,
      job.title,
      job.idea,
      job.status,
      getStage(job),
      epicsById[job.epic_id]?.name,
      getChannelLabel(job),
      getActor(job),
      currentExecutor.label,
      currentExecutor.provider,
    ]
      .map(clean)
      .join(" ")
      .toLocaleLowerCase();
    return (
      (!query || searchable.includes(query)) &&
      (selectedEpic == null ||
        selectedEpic === "all" ||
        String(job.epic_id) === String(selectedEpic)) &&
      matchesChoice([currentExecutor.provider], provider) &&
      matchesChoice([currentExecutor.label, currentExecutor.provider], executor) &&
      matchesChoice([getChannel(job)], channel ?? source) &&
      matchesChoice([getActor(job), getActorLabel(job)], actor)
    );
  });
}
export const matchWorklistFilters = filterJobs;

export function toggleSortKeys(sortKeys, key, multi = false) {
  const current = Array.isArray(sortKeys) ? sortKeys : [];
  const existing = current.find((sort) => sort.key === key);
  if (!multi) {
    return [
      { key, dir: existing && current.length === 1 && existing.dir === "asc" ? "desc" : "asc" },
    ];
  }
  return existing
    ? current.map((sort) =>
        sort.key === key ? { ...sort, dir: sort.dir === "asc" ? "desc" : "asc" } : sort
      )
    : [...current, { key, dir: "asc" }];
}

export function getSortIndicator(sortKeys, key) {
  const index = sortKeys.findIndex((sort) => sort.key === key);
  if (index < 0) return null;
  const arrow = sortKeys[index].dir === "asc" ? "▲" : "▼";
  return sortKeys.length > 1 ? `${arrow}${index + 1}` : arrow;
}

export function getAriaSort(sortKeys, key) {
  const sort = sortKeys.find((candidate) => candidate.key === key);
  if (!sort) return "none";
  return sort.dir === "desc" ? "descending" : "ascending";
}

export function getSortHeaderProps(sortKeys, key, onChange) {
  return {
    "aria-sort": getAriaSort(sortKeys, key),
    onClick: (event) => onChange(toggleSortKeys(sortKeys, key, event.shiftKey)),
  };
}

export function handleJobClick(job, onOpenJob) {
  onOpenJob?.(job.id);
}

export function renderDependencies(job, { mobile = false, onOpenJob } = {}) {
  const dependencies = normalizeDependencies(job);
  if (!dependencies.length) return <span className="job-table-empty">—</span>;
  return (
    <span
      className={`job-dependency-chain${mobile ? " job-dependency-chain-mobile" : ""}`}
      aria-label="Dependency chain"
      style={{
        display: "flex",
        flexDirection: mobile ? "column" : "row",
        flexWrap: mobile ? "nowrap" : "wrap",
        gap: "var(--space-1)",
      }}
    >
      {dependencies.map((dependency, index) => (
        <Fragment key={dependency.id}>
          {index > 0 && <span aria-hidden="true">{mobile ? "↓" : "→"}</span>}
          {onOpenJob ? (
            <button
              type="button"
              className="job-table-epic-chip tap-target"
              onClick={(event) => {
                event.stopPropagation();
                onOpenJob(dependency.id);
              }}
            >
              #{dependency.id}
              {dependency.title ? ` ${dependency.title}` : ""}
            </button>
          ) : (
            <span>
              #{dependency.id}
              {dependency.title ? ` ${dependency.title}` : ""}
            </span>
          )}
        </Fragment>
      ))}
    </span>
  );
}

export function renderCell(job, key, epicsById = {}, onOpenEpic, options = {}) {
  switch (key) {
    case "id":
      return <span className="job-table-id">#{job.id}</span>;
    case "title":
      return (
        <span className="job-table-title" title={job.idea}>
          {job.title || job.idea || "Untitled job"}
        </span>
      );
    case "status": {
      const badge = getStatusBadge(job);
      return (
        <span className={badge.className} title={badge.title}>
          {badge.label || "—"}
        </span>
      );
    }
    case "stage":
      return <span className="job-table-stage">{getStage(job) || "—"}</span>;
    case "priority": {
      const hasBoost = job.effective_priority != null && job.effective_priority !== job.priority;
      const displayPriority = hasBoost ? job.effective_priority : job.priority;
      return (
        <span
          className="job-table-priority"
          title={hasBoost ? describePriorityReasons(job.priority_reasons) : undefined}
        >
          {PRIORITY_LABEL[displayPriority] ?? displayPriority ?? "—"}
        </span>
      );
    }
    case "epic": {
      const epicName = epicsById[job.epic_id]?.name;
      if (onOpenEpic && job.epic_id != null) {
        return (
          <button
            type="button"
            className="job-table-epic job-table-epic-chip tap-target"
            title={epicName}
            onClick={(event) => {
              event.stopPropagation();
              onOpenEpic(job.epic_id);
            }}
          >
            {epicName ?? "—"}
          </button>
        );
      }
      return (
        <span className="job-table-epic" title={epicName}>
          {epicName ?? "—"}
        </span>
      );
    }
    case "channel": {
      const channel = getChannel(job);
      return (
        <span
          className={`badge source-badge ${SOURCE_COLOR[channel] || "source-unknown"}`}
          title={`Filed via ${getChannelLabel(job)}`}
        >
          {getChannelLabel(job)}
        </span>
      );
    }
    // Legacy cell retained for callers that explicitly request "source".
    // Canonical worklists use the separate "channel" and "actor" columns.
    case "source": {
      const { sourceLabel, actorLabel, title } = getSourceInfo(job);
      return (
        <span
          className={`badge source-badge ${SOURCE_COLOR[sourceLabel] || "source-unknown"}`}
          title={title}
        >
          {sourceLabel}
          {actorLabel && (
            <span className="source-actor job-table-source-actor"> · {actorLabel}</span>
          )}
        </span>
      );
    }
    case "actor":
      return (
        <span className="job-table-source-actor" title={getActor(job) || undefined}>
          {getActorLabel(job) || "—"}
        </span>
      );
    case "executor": {
      const executor = getExecutor(job);
      return (
        <span className="job-table-executor" title={executor.provider || undefined}>
          {executor.label || "—"}
        </span>
      );
    }
    case "dependencies":
      return renderDependencies(job, options);
    case "created_at":
    case "updated_at":
      return <span className="job-table-updated">{job[key] ? ago(job[key]) : "—"}</span>;
    default:
      return null;
  }
}

export function WorklistRenderer({
  jobs,
  epicsById = {},
  columns = DEFAULT_DESKTOP_COLUMNS,
  visibleColumns,
  sortKeys = [],
  onSortChange,
  expandedIds = new Set(),
  onToggleExpanded,
  renderDetail,
  onOpenEpic,
  onOpenJob,
  selectedIds,
  onToggleSelected,
  onToggleAll,
  bulkActions = null,
  compact = false,
  mobile: mobileOverride,
}) {
  const detectMobile = () =>
    typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(max-width: 600px)").matches;
  const [detectedMobile, setDetectedMobile] = useState(detectMobile);
  useEffect(() => {
    if (mobileOverride !== undefined || typeof window.matchMedia !== "function") return undefined;
    const query = window.matchMedia("(max-width: 600px)");
    const update = () => setDetectedMobile(query.matches);
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, [mobileOverride]);
  const isMobile = mobileOverride ?? detectedMobile;
  const desktopColumns = visibleColumns ?? columns;
  const selected = selectedIds ?? new Set();
  const selectionEnabled = Boolean(onToggleSelected);
  const allSelected = jobs.length > 0 && jobs.every((job) => selected.has(job.id));
  return (
    <div className={`worklist-renderer${compact ? " worklist-renderer-compact" : ""}`}>
      {selectionEnabled && bulkActions && (
        <div className="worklist-desktop-only">{bulkActions}</div>
      )}
      {isMobile && (
        <div className="worklist-mobile-cards">
          {jobs.length === 0 && (
            <EmptyState icon={ClipboardList} title="No jobs" hint="No jobs match this view." />
          )}
          {jobs.map((job) => {
            const expanded = expandedIds.has(job.id);
            return (
              <article
                key={job.id}
                className={`worklist-mobile-card${expanded ? " expanded" : ""}`}
              >
                <button
                  type="button"
                  className="worklist-mobile-card-main tap-target"
                  onClick={() => handleJobClick(job, onOpenJob)}
                >
                  <span className="job-table-id">#{job.id}</span>
                  <span className="worklist-mobile-title">
                    {job.title || job.idea || "Untitled job"}
                  </span>
                  <span className="worklist-mobile-meta">
                    {renderCell(job, "status", epicsById)}
                    {renderCell(job, "stage", epicsById)}
                    {renderCell(job, "channel", epicsById)}
                    {renderCell(job, "actor", epicsById)}
                    {renderCell(job, "executor", epicsById)}
                  </span>
                </button>
                <div className="worklist-mobile-dependencies">
                  {renderDependencies(job, { mobile: true, onOpenJob })}
                </div>
                {renderDetail && (
                  <button
                    type="button"
                    className="tap-target"
                    aria-expanded={expanded}
                    aria-label={`${expanded ? "Collapse" : "Expand"} job #${job.id}`}
                    onClick={(event) => {
                      event.stopPropagation();
                      onToggleExpanded?.(job.id);
                    }}
                  >
                    <span aria-hidden="true">{expanded ? "▲" : "▼"}</span>
                  </button>
                )}
                {expanded && renderDetail && (
                  <div className="job-table-detail-cell">{renderDetail(job)}</div>
                )}
              </article>
            );
          })}
        </div>
      )}
      {!isMobile && (
        <div className="table-scroll worklist-desktop-table">
          <table className="job-table">
            <thead>
              <tr>
                {renderDetail && <th className="job-table-check" aria-label="Expansion" />}
                {selectionEnabled && (
                  <th className="job-table-check">
                    <input
                      type="checkbox"
                      aria-label="Select all jobs"
                      checked={allSelected}
                      onChange={() => onToggleAll?.(!allSelected, jobs)}
                    />
                  </th>
                )}
                {desktopColumns.map((column) => (
                  <th
                    key={column.key}
                    className="job-table-th sortable"
                    {...getSortHeaderProps(sortKeys, column.key, onSortChange ?? (() => {}))}
                    title={`Sort by ${column.label}`}
                  >
                    {column.label}
                    {getSortIndicator(sortKeys, column.key) && (
                      <span className="sort-indicator">
                        {getSortIndicator(sortKeys, column.key)}
                      </span>
                    )}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {jobs.length === 0 && (
                <tr>
                  <td
                    colSpan={
                      desktopColumns.length + (renderDetail ? 1 : 0) + (selectionEnabled ? 1 : 0)
                    }
                  >
                    <EmptyState
                      icon={ClipboardList}
                      title="No jobs"
                      hint="No jobs match this view."
                    />
                  </td>
                </tr>
              )}
              {jobs.map((job) => {
                const expanded = expandedIds.has(job.id);
                return (
                  <Fragment key={job.id}>
                    <tr
                      className={`job-table-row${expanded ? " expanded" : ""}${selected.has(job.id) ? " selected" : ""}`}
                      onClick={() => handleJobClick(job, onOpenJob)}
                    >
                      {renderDetail && (
                        <td className="job-table-check" data-label="Expand">
                          <button
                            type="button"
                            className="tap-target"
                            aria-label={`${expanded ? "Collapse" : "Expand"} job #${job.id}`}
                            aria-expanded={expanded}
                            onClick={(event) => {
                              event.stopPropagation();
                              onToggleExpanded?.(job.id);
                            }}
                          >
                            <span aria-hidden="true">{expanded ? "▲" : "▼"}</span>
                          </button>
                        </td>
                      )}
                      {selectionEnabled && (
                        <td
                          className="job-table-check worklist-desktop-only"
                          data-label="Select"
                          onClick={(event) => event.stopPropagation()}
                        >
                          <input
                            type="checkbox"
                            aria-label={`Select job #${job.id}`}
                            checked={selected.has(job.id)}
                            onChange={() => onToggleSelected(job.id)}
                          />
                        </td>
                      )}
                      {desktopColumns.map((column) => (
                        <td
                          key={column.key}
                          className={`job-table-td job-table-td-${column.key}`}
                          data-label={column.key === "title" ? undefined : column.label}
                        >
                          {column.render
                            ? column.render(job)
                            : renderCell(job, column.key, epicsById, onOpenEpic)}
                        </td>
                      ))}
                    </tr>
                    {expanded && renderDetail && (
                      <tr className="job-table-detail-row">
                        <td
                          className="job-table-detail-cell"
                          colSpan={
                            desktopColumns.length +
                            (renderDetail ? 1 : 0) +
                            (selectionEnabled ? 1 : 0)
                          }
                        >
                          {renderDetail(job)}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

export const ResponsiveWorklist = WorklistRenderer;
