import { useEffect, useId, useRef, useState } from "react";
import { sortJobs, WorklistRenderer } from "./jobTableShared.jsx";

// One attention-first lane on the Work page: a titled, collapsible table of
// jobs. Empty lanes always collapse to a one-line header so the page never
// shows a full-viewport empty state while other lanes have content.
export function WorkLaneSection({
  title,
  jobs,
  epicsById,
  visibleCols,
  extraColumns = [],
  onOpenEpic,
  onOpenJob,
  onSelectJob,
  selectedJobId = null,
  mobile = false,
  collapsible = false,
  defaultCollapsed = false,
  aboveTable = null,
}) {
  const [manualCollapsed, setManualCollapsed] = useState(defaultCollapsed);
  const [sortKeys, setSortKeys] = useState([
    { key: "attention", dir: "asc" },
    { key: "updated_at", dir: "desc" },
  ]);
  const titleId = useId();
  const contentId = useId();
  const userToggledRef = useRef(false);

  // Track defaultCollapsed as data streams in (e.g. the auto-expand-when-
  // everything-else-is-empty case) until the user explicitly toggles the
  // lane themselves — after that, their choice wins.
  useEffect(() => {
    if (!userToggledRef.current) setManualCollapsed(defaultCollapsed);
  }, [defaultCollapsed]);

  const isEmpty = jobs.length === 0;
  const collapsed = isEmpty || (collapsible && manualCollapsed);
  const cols = [...visibleCols, ...extraColumns];
  const canToggle = collapsible && !isEmpty;

  function toggleCollapsed() {
    userToggledRef.current = true;
    setManualCollapsed((c) => !c);
  }

  const displayJobs = sortJobs(jobs, sortKeys, epicsById);

  return (
    <section className="work-lane">
      <button
        type="button"
        className={`work-lane-header${isEmpty ? " work-lane-header-empty" : ""}${
          canToggle ? " work-lane-header-toggleable" : ""
        }`}
        onClick={() => canToggle && toggleCollapsed()}
        aria-expanded={canToggle ? !collapsed : undefined}
        aria-controls={canToggle ? contentId : undefined}
        disabled={isEmpty}
      >
        <span id={titleId} className="work-lane-title">
          {title}
        </span>
        <span className="work-lane-header-right">
          <span className="badge work-lane-count">{jobs.length}</span>
          {canToggle && <span className="work-lane-caret">{manualCollapsed ? "▸" : "▾"}</span>}
        </span>
      </button>

      {!collapsed && (
        <div id={contentId} className="work-lane-content" role="region" aria-labelledby={titleId}>
          {aboveTable}
          <WorklistRenderer
            jobs={displayJobs}
            epicsById={epicsById}
            visibleColumns={cols}
            sortKeys={sortKeys}
            onSortChange={setSortKeys}
            onOpenEpic={onOpenEpic}
            onOpenJob={(jobId) => (mobile ? onOpenJob?.(jobId) : onSelectJob?.(jobId))}
            selectedIds={selectedJobId == null ? new Set() : new Set([selectedJobId])}
            compact
            mobile={mobile}
          />
        </div>
      )}
    </section>
  );
}
