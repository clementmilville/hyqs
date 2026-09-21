import { useEffect, useRef, useState } from "react";
import { JobDetailTabs } from "./JobDetailTabs.jsx";

const PRACTICAL_MIN_WIDTH = 360;
const DEFAULT_MAX_WIDTH = 720;
const EXPANDED_MAX_WIDTH = 1100;
const KEYBOARD_RESIZE_STEP = 20;

function widthBounds(viewportWidth) {
  const maximum = Math.min(EXPANDED_MAX_WIDTH, viewportWidth * 0.8);
  return {
    minimum: Math.min(PRACTICAL_MIN_WIDTH, maximum),
    maximum,
    initial: Math.min(DEFAULT_MAX_WIDTH, viewportWidth * 0.45),
  };
}

function boundedWidth(value, bounds) {
  return Math.min(bounds.maximum, Math.max(bounds.minimum, value));
}

function present(value, fallback = "Not available") {
  return value == null || value === "" ? fallback : String(value);
}

function executorLabel(executor) {
  if (!executor) return "No current executor";
  return present(
    executor.label || executor.agent_name || executor.name || executor.provider,
    "No current executor"
  );
}

function historicalExecutors(events) {
  return [
    ...new Set(
      events
        .map((event) => {
          const name = event.agent_name || event.agent_id;
          const detail = event.agent_model || event.agent_provider;
          if (!name && !detail) return null;
          return [event.stage, name, detail].filter(Boolean).join(" · ");
        })
        .filter(Boolean)
    ),
  ];
}

function lineageLabel(item) {
  if (typeof item === "string" || typeof item === "number") return String(item);
  const id = item.job_id ?? item.id;
  const relation = item.relationship || item.relation || item.kind;
  return [relation, id != null ? `job #${id}` : null, item.title].filter(Boolean).join(" · ");
}

export function WorkJobInspector({
  job,
  project,
  events,
  workerContext,
  lineage = [],
  onOpenJob,
  onClose,
  openerRef,
  mode = "desktop",
  initialWidth,
}) {
  const drawerRef = useRef(null);
  const closeRef = useRef(null);
  const resizeStart = useRef(null);
  const isOverlay = mode === "tablet";
  const [bounds, setBounds] = useState(() => widthBounds(window.innerWidth));
  const [width, setWidth] = useState(() => {
    const initialBounds = widthBounds(window.innerWidth);
    return boundedWidth(initialWidth ?? initialBounds.initial, initialBounds);
  });

  useEffect(() => {
    if (isOverlay) return undefined;
    const handleViewportResize = () => {
      const nextBounds = widthBounds(window.innerWidth);
      setBounds(nextBounds);
      setWidth((value) => boundedWidth(value, nextBounds));
    };
    window.addEventListener("resize", handleViewportResize);
    return () => window.removeEventListener("resize", handleViewportResize);
  }, [isOverlay]);

  useEffect(() => {
    if (!isOverlay) return undefined;
    const opener = openerRef?.current || document.activeElement;
    const drawer = drawerRef.current;
    const focusable = () =>
      [
        ...drawer.querySelectorAll(
          'button, [href], input, select, textarea, [tabindex]:not([tabindex="-1"])'
        ),
      ].filter((element) => !element.disabled);

    closeRef.current?.focus();
    const handleKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose?.();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable();
      if (!items.length) {
        event.preventDefault();
        drawer.focus();
      } else if (event.shiftKey && document.activeElement === items[0]) {
        event.preventDefault();
        items.at(-1).focus();
      } else if (!event.shiftKey && document.activeElement === items.at(-1)) {
        event.preventDefault();
        items[0].focus();
      }
    };
    drawer.addEventListener("keydown", handleKeyDown);
    return () => {
      drawer.removeEventListener("keydown", handleKeyDown);
      opener?.focus?.();
    };
  }, [isOverlay, onClose, openerRef]);

  function beginResize(event) {
    event.preventDefault();
    resizeStart.current = { x: event.clientX, width };
    event.currentTarget.setPointerCapture?.(event.pointerId);
  }

  function resize(event) {
    if (!resizeStart.current) return;
    setWidth(
      boundedWidth(resizeStart.current.width + resizeStart.current.x - event.clientX, bounds)
    );
  }

  function endResize(event) {
    resizeStart.current = null;
    event.currentTarget.releasePointerCapture?.(event.pointerId);
  }

  const history = historicalExecutors(events ?? []);
  const blocker = job.blocker || job.error;
  const nextAction = job.next_action || events?.at(-1)?.detail?.next_action;
  const source = [job.source, job.source_actor].filter(Boolean).join(" · ");
  const worker =
    typeof workerContext === "string"
      ? workerContext
      : workerContext &&
        [workerContext.name || workerContext.id, workerContext.status, workerContext.host]
          .filter(Boolean)
          .join(" · ");

  return (
    <aside
      ref={drawerRef}
      className={`work-job-inspector work-job-inspector-${mode}`}
      style={isOverlay ? undefined : { width }}
      role={isOverlay ? "dialog" : "complementary"}
      aria-modal={isOverlay ? "true" : undefined}
      aria-labelledby={`work-job-inspector-title-${job.id}`}
      tabIndex={-1}
    >
      {!isOverlay && (
        <div
          role="separator"
          aria-label="Resize job inspector"
          title="Resize job inspector"
          aria-orientation="vertical"
          aria-valuemin={Math.round(bounds.minimum)}
          aria-valuemax={Math.round(bounds.maximum)}
          aria-valuenow={width}
          className="work-job-inspector-resize"
          tabIndex={0}
          onPointerDown={beginResize}
          onPointerMove={resize}
          onPointerUp={endResize}
          onPointerCancel={endResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") {
              event.preventDefault();
              setWidth((value) => boundedWidth(value + KEYBOARD_RESIZE_STEP, bounds));
            }
            if (event.key === "ArrowRight") {
              event.preventDefault();
              setWidth((value) => boundedWidth(value - KEYBOARD_RESIZE_STEP, bounds));
            }
          }}
        />
      )}
      <header className="work-job-inspector-header">
        <button
          ref={closeRef}
          type="button"
          className="btn-secondary"
          onClick={onClose}
          aria-label="Close inspector"
        >
          Close
        </button>
        <h2 id={`work-job-inspector-title-${job.id}`}>
          <button
            type="button"
            className="work-job-inspector-title"
            onClick={() => onOpenJob?.(job.id)}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                onOpenJob?.(job.id);
              }
            }}
            title={`Open full details for Job #${job.id}`}
          >
            <span>Job #{job.id}</span>
            {(job.title || job.idea) && <span>{job.title || job.idea}</span>}
          </button>
        </h2>
      </header>

      <div
        className="work-job-inspector-content"
        role="region"
        aria-label={`Job #${job.id} inspector content`}
      >
        <section aria-labelledby={`provenance-${job.id}`}>
          <h3 id={`provenance-${job.id}`}>Filing provenance</h3>
          <p>{present(source)}</p>
        </section>
        <section aria-labelledby={`action-${job.id}`}>
          <h3 id={`action-${job.id}`}>{blocker ? "Blocker" : "Next action"}</h3>
          <p>{present(blocker || nextAction, "No next action recorded")}</p>
        </section>
        <section aria-labelledby={`executor-${job.id}`}>
          <h3 id={`executor-${job.id}`}>Current executor</h3>
          <p>{executorLabel(job.current_executor)}</p>
        </section>
        <section aria-labelledby={`worker-${job.id}`}>
          <h3 id={`worker-${job.id}`}>Current worker context</h3>
          <p>{present(worker, "No worker assigned")}</p>
        </section>
        <section aria-labelledby={`history-${job.id}`}>
          <h3 id={`history-${job.id}`}>Prior stage agents and providers</h3>
          {history.length ? (
            <ul>
              {history.map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          ) : (
            <p>None recorded</p>
          )}
        </section>
        <section aria-labelledby={`lineage-${job.id}`}>
          <h3 id={`lineage-${job.id}`}>Remediation lineage</h3>
          {lineage.length ? (
            <ul>
              {lineage.map((item, index) => (
                <li key={`${lineageLabel(item)}-${index}`}>{lineageLabel(item)}</li>
              ))}
            </ul>
          ) : (
            <p>No remediation relationships</p>
          )}
        </section>

        <JobDetailTabs
          job={job}
          project={project}
          events={events}
          onOpenJob={onOpenJob}
          inspectorMode
        />
      </div>
    </aside>
  );
}
