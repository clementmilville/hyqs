import { useEffect, useRef, useState } from "react";
import { listDecisions, getDecision } from "../api.js";
import { MarkdownContent } from "./MarkdownContent.jsx";
import { EmptyState } from "./EmptyState.jsx";
import { usePageData } from "../hooks/usePageData.js";
import { PageState } from "./PageState.jsx";

function DecisionRow({ decision, expanded, onToggle, content, loading }) {
  return (
    <div className="decision-row">
      <div className="table-scroll">
        <button
          className="decision-row-header tap-target"
          onClick={onToggle}
          aria-expanded={expanded}
        >
          <span className="decision-job-badge">#{decision.job_id}</span>
          <span className="decision-title">{decision.title}</span>
          <span className="decision-date">{decision.date}</span>
          <span className="decision-expand-icon" aria-hidden="true">
            {expanded ? "▲" : "▼"}
          </span>
        </button>
      </div>
      {expanded && (
        <div className="decision-row-body">
          {loading ? <p className="hint">Loading…</p> : <MarkdownContent content={content} />}
        </div>
      )}
    </div>
  );
}

export function DecisionsList({ projectId, epicId }) {
  const {
    data: decisions,
    loading,
    forbidden,
    error,
    retry,
  } = usePageData(() => listDecisions(projectId, epicId), [projectId, epicId]);
  const [expandedFilename, setExpandedFilename] = useState(null);
  const [loadingFilename, setLoadingFilename] = useState(null);
  const cacheRef = useRef(new Map());
  const [, forceRender] = useState(0);

  useEffect(() => {
    setExpandedFilename(null);
    cacheRef.current = new Map();
  }, [projectId, epicId]);

  async function handleToggle(decision) {
    const { filename } = decision;
    if (expandedFilename === filename) {
      setExpandedFilename(null);
      return;
    }
    setExpandedFilename(filename);
    if (cacheRef.current.has(filename)) return;
    setLoadingFilename(filename);
    try {
      const resp = await getDecision(projectId, filename);
      cacheRef.current.set(filename, resp.content);
      forceRender((n) => n + 1);
    } catch (e) {
      cacheRef.current.set(filename, `⚠️ Failed to load: ${e.message}`);
      forceRender((n) => n + 1);
    } finally {
      setLoadingFilename(null);
    }
  }

  return (
    <PageState forbidden={forbidden} error={error} loading={loading} retry={retry}>
      {decisions && decisions.length === 0 ? (
        <EmptyState
          icon="📝"
          title="No decisions yet"
          hint="Decisions are recorded automatically when jobs merge."
        />
      ) : (
        <div className="decisions-list">
          {decisions?.map((d) => (
            <DecisionRow
              key={d.filename}
              decision={d}
              expanded={expandedFilename === d.filename}
              onToggle={() => handleToggle(d)}
              content={cacheRef.current.get(d.filename) ?? ""}
              loading={loadingFilename === d.filename}
            />
          ))}
        </div>
      )}
    </PageState>
  );
}
