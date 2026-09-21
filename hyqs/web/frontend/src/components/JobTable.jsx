import { useEffect, useRef, useState } from "react";
import { GatedAction } from "../context.js";
import { archiveJob, listJobsFiltered, patchJobEpic } from "../api.js";
import { FilterPresets } from "./FilterPresets.jsx";
import { BulkActionBar } from "./BulkActionBar.jsx";
import { JobDetailTabs } from "./JobDetailTabs.jsx";
import { useToast } from "./Toast.jsx";
import {
  STATUS_FILTER_CHIPS,
  STATUS_FILTER_LABEL,
  DEFAULT_DESKTOP_COLUMNS,
  filterJobs,
  getActor,
  getChannel,
  getExecutor,
  sortJobs,
  WorklistRenderer,
} from "./jobTableShared.jsx";

export function JobTable({
  jobs,
  epics,
  project,
  selectedEpicId,
  onNewJob,
  onChanged,
  onOpenEpic,
  onOpenJob,
  initialStatusFilter = "all",
}) {
  const toast = useToast();
  const colStorageKey = `work_columns_${project.id}`;

  const [statusFilter, setStatusFilter] = useState(initialStatusFilter);
  const [searchQuery, setSearchQuery] = useState("");
  const [providerFilter, setProviderFilter] = useState("");
  const [channelFilter, setChannelFilter] = useState("");
  const [actorFilter, setActorFilter] = useState("");
  const [sortKeys, setSortKeys] = useState([
    { key: "attention", dir: "asc" },
    { key: "updated_at", dir: "desc" },
  ]);
  const [hiddenCols, setHiddenCols] = useState(() => {
    try {
      return new Set(JSON.parse(localStorage.getItem(colStorageKey) || "[]"));
    } catch {
      return new Set();
    }
  });
  const [colDropOpen, setColDropOpen] = useState(false);
  const [extraJobs, setExtraJobs] = useState([]);
  const [refreshKey, setRefreshKey] = useState(0);
  const [selectedIds, setSelectedIds] = useState(new Set());
  const [expandedIds, setExpandedIds] = useState(new Set());
  const colDropRef = useRef(null);

  const epicsById = Object.fromEntries(epics.map((ep) => [ep.id, ep]));

  useEffect(() => {
    function handleClick(e) {
      if (colDropRef.current && !colDropRef.current.contains(e.target)) {
        setColDropOpen(false);
      }
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, []);

  useEffect(() => {
    if (statusFilter === "active") {
      setExtraJobs([]);
      return;
    }
    let live = true;
    const load = () =>
      listJobsFiltered(statusFilter, project.id)
        .then((j) => {
          if (live) setExtraJobs(j);
        })
        .catch(() => {});
    load();
    const id = setInterval(load, 5000);
    return () => {
      live = false;
      clearInterval(id);
    };
  }, [statusFilter, project.id, refreshKey]);

  const triggerRefresh = () => {
    setRefreshKey((k) => k + 1);
    onChanged?.();
  };

  function toggleCol(key) {
    setHiddenCols((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      localStorage.setItem(colStorageKey, JSON.stringify([...next]));
      return next;
    });
  }

  const baseJobs = statusFilter === "active" ? jobs : extraJobs;

  const filteredJobs = filterJobs(
    baseJobs,
    {
      searchQuery,
      epicId: selectedEpicId,
      provider: providerFilter,
      channel: channelFilter,
      actor: actorFilter,
    },
    epicsById
  );
  const providers = [...new Set(baseJobs.map((job) => getExecutor(job).provider).filter(Boolean))];
  const channels = [...new Set(baseJobs.map(getChannel).filter(Boolean))];
  const actors = [...new Set(baseJobs.map(getActor).filter(Boolean))];

  const displayJobs = sortJobs(filteredJobs, sortKeys, epicsById);

  const visibleCols = DEFAULT_DESKTOP_COLUMNS.filter((c) => !hiddenCols.has(c.key));

  const allSelected = displayJobs.length > 0 && displayJobs.every((j) => selectedIds.has(j.id));

  function toggleAll() {
    if (allSelected) {
      setSelectedIds(new Set());
    } else {
      setSelectedIds(new Set(displayJobs.map((j) => j.id)));
    }
  }

  function toggleRow(id) {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  function toggleExpand(id) {
    setExpandedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function handleBulkArchive(ids) {
    await Promise.all(ids.map((id) => archiveJob(id).catch(() => {})));
    setSelectedIds(new Set());
    triggerRefresh();
    toast.success(`Archived ${ids.length} job${ids.length === 1 ? "" : "s"}`);
  }

  async function handleBulkMoveEpic(ids, epicId) {
    await Promise.all(ids.map((id) => patchJobEpic(id, epicId).catch(() => {})));
    setSelectedIds(new Set());
    triggerRefresh();
    toast.success(`Moved ${ids.length} job${ids.length === 1 ? "" : "s"}`);
  }

  const currentFilters = {
    statusFilter,
    searchQuery,
    sortKeys,
    selectedEpicId,
    providerFilter,
    channelFilter,
    actorFilter,
    hiddenCols: [...hiddenCols],
  };

  function loadPreset(state) {
    if (state.statusFilter) setStatusFilter(state.statusFilter);
    if (state.searchQuery !== undefined) setSearchQuery(state.searchQuery);
    if (state.sortKeys) setSortKeys(state.sortKeys);
    if (state.providerFilter !== undefined) setProviderFilter(state.providerFilter);
    if (state.channelFilter !== undefined) setChannelFilter(state.channelFilter);
    if (state.actorFilter !== undefined) setActorFilter(state.actorFilter);
    if (state.hiddenCols) setHiddenCols(new Set(state.hiddenCols));
  }

  return (
    <div className="job-table-wrap">
      <div className="job-table-toolbar">
        <div className="filter-bar">
          {STATUS_FILTER_CHIPS.map((s) => (
            <button
              key={s}
              className={`tap-target${statusFilter === s ? " active" : ""}`}
              onClick={() => {
                setStatusFilter(s);
                setSearchQuery("");
                setSelectedIds(new Set());
              }}
            >
              {STATUS_FILTER_LABEL[s]}
            </button>
          ))}
          <input
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            placeholder="Search…"
          />
          <select
            aria-label="Provider or executor"
            value={providerFilter}
            onChange={(e) => setProviderFilter(e.target.value)}
          >
            <option value="">All providers</option>
            {providers.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            aria-label="Channel"
            value={channelFilter}
            onChange={(e) => setChannelFilter(e.target.value)}
          >
            <option value="">All channels</option>
            {channels.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
          <select
            aria-label="Actor"
            value={actorFilter}
            onChange={(e) => setActorFilter(e.target.value)}
          >
            <option value="">All actors</option>
            {actors.map((value) => (
              <option key={value} value={value}>
                {value}
              </option>
            ))}
          </select>
        </div>

        <div className="job-table-toolbar-row2">
          <div className="job-table-toolbar-right">
            <div className="col-vis-wrap" ref={colDropRef}>
              <button
                className="btn-secondary col-vis-btn tap-target"
                onClick={() => setColDropOpen((v) => !v)}
              >
                View
              </button>
              {colDropOpen && (
                <div className="col-vis-dropdown">
                  <strong>Columns</strong>
                  {DEFAULT_DESKTOP_COLUMNS.map((c) => (
                    <label key={c.key} className="col-vis-item">
                      <input
                        type="checkbox"
                        checked={!hiddenCols.has(c.key)}
                        onChange={() => toggleCol(c.key)}
                      />
                      {c.label}
                    </label>
                  ))}
                  <strong>Saved views</strong>
                  <FilterPresets
                    projectId={project.id}
                    currentFilters={currentFilters}
                    onLoad={loadPreset}
                  />
                </div>
              )}
            </div>
            <GatedAction require="queue_job">
              <button className="tap-target" onClick={onNewJob}>
                + New Job
              </button>
            </GatedAction>
          </div>
        </div>

        {selectedIds.size > 0 && (
          <BulkActionBar
            selectedIds={[...selectedIds]}
            epics={epics}
            onArchive={handleBulkArchive}
            onMoveToEpic={handleBulkMoveEpic}
            onClearSelection={() => setSelectedIds(new Set())}
          />
        )}
      </div>

      <WorklistRenderer
        jobs={displayJobs}
        epicsById={epicsById}
        visibleColumns={visibleCols}
        sortKeys={sortKeys}
        onSortChange={setSortKeys}
        expandedIds={expandedIds}
        onToggleExpanded={toggleExpand}
        renderDetail={(job) => <JobDetailTabs job={job} />}
        onOpenEpic={onOpenEpic}
        onOpenJob={onOpenJob}
        selectedIds={selectedIds}
        onToggleSelected={toggleRow}
        onToggleAll={toggleAll}
      />
    </div>
  );
}
