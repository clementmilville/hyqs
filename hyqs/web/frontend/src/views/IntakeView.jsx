import { useEffect, useRef, useState } from "react";
import {
  createIntakeSession,
  listIntakeSessions,
  getIntakeSession,
  confirmIntake,
  streamIntakeMessage,
  abandonIntake,
  setDraftSpec as patchDraftSpec,
  provisionProject,
} from "../api.js";
import { useAIStream } from "../hooks/useAIStream.js";
import { ActivityFeed } from "../components/ActivityFeed.jsx";
import IntakeBlocks from "../components/IntakeBlocks.jsx";
import { ThinkingIndicator } from "../components/ThinkingIndicator.jsx";
import { TokenTally } from "../components/TokenTally.jsx";
import { ago } from "../utils.js";

const INTAKE_REQUIRED = [
  "name",
  "slug",
  "one_liner",
  "problem",
  "users",
  "auth",
  "features",
  "data_model",
  "stack",
];

const SCALAR_FIELDS = INTAKE_REQUIRED.filter((f) => f !== "features");

const INTAKE_PHASES = [
  "Vision",
  "Auth & Access",
  "Features",
  "Data & Integrations",
  "Constraints",
  "Stack & Review",
];

const BLANK_FEATURE = {
  title: "",
  description: "",
  acceptance_criteria: "",
  theme: "core",
  source: "user",
};

const FEATURE_FORM_STYLE = {
  display: "flex",
  flexWrap: "wrap",
  gap: "var(--space-2)",
  padding: "var(--space-4)",
  borderRadius: "var(--radius-md)",
  boxShadow: "var(--shadow-sm)",
  background: "var(--color-surface)",
};

function FeatureForm({ draft, onChange, onSave, onCancel, error, disabled }) {
  return (
    <div className="feature-form" style={FEATURE_FORM_STYLE}>
      <label className="sr-only" htmlFor="feature-title">
        Title
      </label>
      <input
        id="feature-title"
        placeholder="Title"
        value={draft.title}
        onChange={(e) => onChange({ ...draft, title: e.target.value })}
        disabled={disabled}
        style={{ minHeight: "var(--tap-min)" }}
      />
      <label className="sr-only" htmlFor="feature-description">
        Description
      </label>
      <textarea
        id="feature-description"
        placeholder="Description"
        value={draft.description}
        onChange={(e) => onChange({ ...draft, description: e.target.value })}
        disabled={disabled}
      />
      <label className="sr-only" htmlFor="feature-acceptance-criteria">
        Acceptance criteria
      </label>
      <input
        id="feature-acceptance-criteria"
        placeholder="Acceptance criteria"
        value={draft.acceptance_criteria}
        onChange={(e) => onChange({ ...draft, acceptance_criteria: e.target.value })}
        disabled={disabled}
        style={{ minHeight: "var(--tap-min)" }}
      />
      <label className="sr-only" htmlFor="feature-theme">
        Theme
      </label>
      <input
        id="feature-theme"
        placeholder="Theme"
        value={draft.theme}
        onChange={(e) => onChange({ ...draft, theme: e.target.value })}
        disabled={disabled}
        style={{ minHeight: "var(--tap-min)" }}
      />
      <label className="sr-only" htmlFor="feature-source">
        Source
      </label>
      <input
        id="feature-source"
        placeholder="Source"
        value={draft.source}
        onChange={(e) => onChange({ ...draft, source: e.target.value })}
        disabled={disabled}
        style={{ minHeight: "var(--tap-min)" }}
      />
      {error && <span className="field-error">{error}</span>}
      <button className="tap-target btn-secondary" onClick={onSave} disabled={disabled}>
        Save
      </button>
      <button className="tap-target btn-secondary" onClick={onCancel} disabled={disabled}>
        Cancel
      </button>
    </div>
  );
}

function countFilled(draft) {
  return INTAKE_REQUIRED.filter((f) => {
    const v = draft[f];
    return v && (!Array.isArray(v) || v.length > 0);
  }).length;
}

export function IntakeView({ onNavWorkspace, onCancel: onCancelProp }) {
  const handleCancel =
    onCancelProp ?? (onNavWorkspace ? () => onNavWorkspace(null, "dashboard") : null);
  const [sessionId, setSessionId] = useState(null);
  const [updatedAt, setUpdatedAt] = useState(null);
  const [messages, setMessages] = useState([]);
  const [draftSpec, setDraftSpec] = useState({});
  const [missing, setMissing] = useState(INTAKE_REQUIRED);
  const [currentPhase, setCurrentPhase] = useState(null);
  const [initLoading, setInitLoading] = useState(false);
  const [inputText, setInputText] = useState("");
  const [error, setError] = useState("");
  const [specOpen, setSpecOpen] = useState(false);
  const [countdown, setCountdown] = useState(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState("");
  const [receipt, setReceipt] = useState(null);
  // null = still initialising, [] = no in-progress sessions, [...] = picker open
  const [pickerSessions, setPickerSessions] = useState(null);

  const [showJump, setShowJump] = useState(false);

  // Skip-interview blank-project state
  const [skipOpen, setSkipOpen] = useState(false);
  const [skipName, setSkipName] = useState("");
  const [skipDesc, setSkipDesc] = useState("");
  const [skipBusy, setSkipBusy] = useState(false);

  // Inline edit state for scalar fields
  const [editingField, setEditingField] = useState(null);
  const [fieldDraft, setFieldDraft] = useState("");
  const [fieldStatus, setFieldStatus] = useState({});

  // Feature editing state
  const [editingFeatureIdx, setEditingFeatureIdx] = useState(null);
  const [featureDraft, setFeatureDraft] = useState(BLANK_FEATURE);
  const [featSaveError, setFeatSaveError] = useState("");

  const endRef = useRef(null);
  const userScrolledUp = useRef(false);
  const prevStreamingRef = useRef(false);
  const lastMsgRef = useRef(null);
  const countdownTriggeredRef = useRef(false);
  const countdownIntervalRef = useRef(null);
  const autoConfirmCalledRef = useRef(false);

  const {
    streaming,
    reconnecting,
    text: aiText,
    toolEvents,
    error: aiError,
    result,
    usage,
    start,
    stop,
  } = useAIStream();

  useEffect(() => {
    setInitLoading(true);
    listIntakeSessions()
      .then(({ sessions }) => {
        const inProgress = sessions.filter((s) => s.status === "in_progress");
        if (inProgress.length > 0) {
          setPickerSessions(inProgress);
          setInitLoading(false);
        } else {
          return createIntakeSession().then(({ session }) => {
            setSessionId(session.session_id);
            setUpdatedAt(session.updated_at);
            setPickerSessions([]);
            setInitLoading(false);
          });
        }
      })
      .catch((err) => {
        setError(err.message);
        setInitLoading(false);
      });
  }, []);

  useEffect(() => {
    function handleScroll() {
      const atBottom = window.scrollY + window.innerHeight >= document.body.scrollHeight - 80;
      userScrolledUp.current = !atBottom;
      setShowJump(!atBottom);
    }
    window.addEventListener("scroll", handleScroll, { passive: true });
    return () => window.removeEventListener("scroll", handleScroll);
  }, []);

  useEffect(() => {
    if (!userScrolledUp.current) {
      endRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, aiText]);

  useEffect(() => {
    const wasStreaming = prevStreamingRef.current;
    prevStreamingRef.current = streaming;
    if (!wasStreaming || streaming) return;

    if (result) {
      setMessages((m) => [
        ...m,
        { role: "assistant", text: result.text || "", blocks: result.blocks || null },
      ]);
      setDraftSpec(result.draft_spec || draftSpec);
      setMissing(result.missing || INTAKE_REQUIRED);
      setCurrentPhase(result.phase || null);
      if (result.session_updated_at) setUpdatedAt(result.session_updated_at);
    }
    // aiError is rendered inline with a Retry button — no setError here
  }, [streaming]);

  // Start countdown when spec-complete signal arrives
  useEffect(() => {
    if (!result?.complete) return;
    if (countdownTriggeredRef.current) return;
    countdownTriggeredRef.current = true;
    setCountdown(5);
    countdownIntervalRef.current = setInterval(() => {
      setCountdown((c) => (c !== null && c > 0 ? c - 1 : c));
    }, 1000);
  }, [result]);

  // Fire autoConfirm when countdown reaches 0; clean up interval on unmount
  useEffect(() => {
    if (countdown !== 0) return;
    if (countdownIntervalRef.current) {
      clearInterval(countdownIntervalRef.current);
      countdownIntervalRef.current = null;
    }
    autoConfirm();
  }, [countdown]);

  // Clear the countdown interval on unmount
  useEffect(() => {
    return () => {
      if (countdownIntervalRef.current) {
        clearInterval(countdownIntervalRef.current);
        countdownIntervalRef.current = null;
      }
    };
  }, []);

  // Auto-navigate to the project board 3 seconds after receipt renders
  useEffect(() => {
    if (!receipt) return;
    const id = setTimeout(() => {
      onNavWorkspace(receipt.project.id, "dashboard");
    }, 3000);
    return () => clearTimeout(id);
  }, [receipt]);

  async function autoConfirm() {
    if (!sessionId || autoConfirmCalledRef.current) return;
    autoConfirmCalledRef.current = true;
    setConfirming(true);
    setError("");
    try {
      const data = await confirmIntake(sessionId);
      setReceipt({ project: data.project, plan: data.plan });
    } catch (err) {
      setError(err.message);
    } finally {
      setConfirming(false);
    }
  }

  async function manualConfirm() {
    if (!sessionId || autoConfirmCalledRef.current) return;
    autoConfirmCalledRef.current = true;
    setConfirming(true);
    setConfirmError("");
    try {
      const data = await confirmIntake(sessionId);
      setReceipt({ project: data.project, plan: data.plan });
    } catch (err) {
      autoConfirmCalledRef.current = false;
      const missingFields = err.body?.missing;
      setConfirmError(
        missingFields && missingFields.length
          ? `${err.message}: ${missingFields.join(", ")}`
          : err.message
      );
    } finally {
      setConfirming(false);
    }
  }

  function cancelCountdown() {
    if (countdownIntervalRef.current) {
      clearInterval(countdownIntervalRef.current);
      countdownIntervalRef.current = null;
    }
    setCountdown(null);
  }

  function send(e) {
    e.preventDefault();
    if (!inputText.trim() || streaming || initLoading || !sessionId) return;
    const msg = inputText.trim();
    setInputText("");
    setError("");
    setMessages((m) => [...m, { role: "user", text: msg }]);
    lastMsgRef.current = msg;
    start(streamIntakeMessage, sessionId, msg);
  }

  function retry() {
    if (!lastMsgRef.current || !sessionId) return;
    start(streamIntakeMessage, sessionId, lastMsgRef.current);
  }

  async function resumeSession(sid) {
    setInitLoading(true);
    setError("");
    try {
      const { session } = await getIntakeSession(sid);
      setSessionId(session.session_id);
      setUpdatedAt(session.updated_at);
      setMessages((session.messages || []).map((m) => ({ role: m.role, text: m.content || "" })));
      setDraftSpec(session.draft_spec || {});
      setMissing(session.missing_required ?? INTAKE_REQUIRED);
      setCurrentPhase(null);
      setPickerSessions([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setInitLoading(false);
    }
  }

  async function startFresh() {
    setInitLoading(true);
    setError("");
    try {
      const { session } = await createIntakeSession();
      setSessionId(session.session_id);
      setUpdatedAt(session.updated_at);
      setPickerSessions([]);
    } catch (err) {
      setError(err.message);
    } finally {
      setInitLoading(false);
    }
  }

  async function abandonSession(sid) {
    try {
      await abandonIntake(sid);
      const next = pickerSessions.filter((s) => s.session_id !== sid);
      setPickerSessions(next);
      if (next.length === 0) {
        await startFresh();
      }
    } catch (err) {
      setError(err.message);
    }
  }

  async function skipProvision(e) {
    e.preventDefault();
    if (!skipName.trim() || skipBusy) return;
    setSkipBusy(true);
    setError("");
    try {
      const res = await provisionProject(skipName.trim(), skipDesc.trim());
      onNavWorkspace(res.project.id, "dashboard");
    } catch (err) {
      setError(err.message);
      setSkipBusy(false);
    }
  }

  function startEditField(fieldName) {
    if (streaming) return;
    setEditingField(fieldName);
    setFieldDraft(String(draftSpec[fieldName] || ""));
  }

  async function commitField() {
    const fieldName = editingField;
    if (!fieldName || !sessionId) return;
    const val = fieldDraft.trim();
    setEditingField(null);
    try {
      const res = await patchDraftSpec(sessionId, { [fieldName]: val }, updatedAt);
      setDraftSpec(res.session.draft_spec);
      setMissing(res.missing);
      setUpdatedAt(res.session.updated_at);
      setFieldStatus((prev) => ({ ...prev, [fieldName]: "saved" }));
      setTimeout(() => setFieldStatus((prev) => ({ ...prev, [fieldName]: null })), 2000);
    } catch (err) {
      setFieldStatus((prev) => ({ ...prev, [fieldName]: { error: err.message } }));
    }
  }

  function onFieldKeyDown(e) {
    if (e.key === "Enter") {
      e.preventDefault();
      commitField();
    }
    if (e.key === "Escape") setEditingField(null);
  }

  async function saveFeatures(newFeatures) {
    if (!sessionId) return;
    setFeatSaveError("");
    try {
      const res = await patchDraftSpec(sessionId, { features: newFeatures }, updatedAt);
      setDraftSpec(res.session.draft_spec);
      setMissing(res.missing);
      setUpdatedAt(res.session.updated_at);
      setEditingFeatureIdx(null);
      setFeatureDraft(BLANK_FEATURE);
    } catch (err) {
      setFeatSaveError(err.message);
    }
  }

  function startAddFeature() {
    if (streaming) return;
    setEditingFeatureIdx("new");
    setFeatureDraft(BLANK_FEATURE);
    setFeatSaveError("");
  }

  function startEditFeature(idx) {
    if (streaming) return;
    setEditingFeatureIdx(idx);
    setFeatureDraft({ ...BLANK_FEATURE, ...(draftSpec.features || [])[idx] });
    setFeatSaveError("");
  }

  async function removeFeature(idx) {
    const newFeatures = (draftSpec.features || []).filter((_, i) => i !== idx);
    await saveFeatures(newFeatures);
  }

  async function acceptFeature(idx) {
    const newFeatures = (draftSpec.features || []).map((f, i) =>
      i === idx ? { ...f, source: "user" } : f
    );
    await saveFeatures(newFeatures);
  }

  async function commitFeature() {
    const features = draftSpec.features || [];
    const newFeatures =
      editingFeatureIdx === "new"
        ? [...features, featureDraft]
        : features.map((f, i) => (i === editingFeatureIdx ? featureDraft : f));
    await saveFeatures(newFeatures);
  }

  const loading = initLoading || streaming;
  const specFilled = INTAKE_REQUIRED.length - missing.length;

  // Show session picker when there are in-progress sessions to choose from
  if (pickerSessions && pickerSessions.length > 0) {
    return (
      <div className="intake">
        <div
          className="intake-header"
          style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-3)" }}
        >
          {handleCancel && (
            <button className="intake-back-btn tap-target btn-secondary" onClick={handleCancel}>
              ← Back
            </button>
          )}
          <h2 className="intake-title">New Project</h2>
        </div>
        {error && <p className="err">{error}</p>}
        <div className="intake-picker">
          <p className="picker-hint">
            You have in-progress intake sessions. Resume one or start fresh.
          </p>
          {pickerSessions.map((s) => (
            <div
              key={s.session_id}
              className="picker-row"
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: "var(--space-2)",
                padding: "var(--space-2) 0",
              }}
            >
              <span className="picker-name">{s.draft_spec?.name || "(unnamed)"}</span>
              <span className="picker-time">{ago(s.updated_at)}</span>
              <span className="badge picker-progress">
                {countFilled(s.draft_spec || {})}/{INTAKE_REQUIRED.length}
              </span>
              <button
                className="picker-resume-btn tap-target btn-secondary"
                disabled={initLoading}
                onClick={() => resumeSession(s.session_id)}
              >
                Resume
              </button>
              <button
                className="picker-abandon-btn tap-target btn-ghost-danger"
                disabled={initLoading}
                onClick={() => abandonSession(s.session_id)}
              >
                Abandon
              </button>
            </div>
          ))}
          <button
            className="picker-fresh-btn tap-target btn-secondary"
            disabled={initLoading}
            onClick={startFresh}
          >
            Start Fresh
          </button>
          <button
            className="skip-interview-toggle tap-target btn-secondary"
            disabled={initLoading}
            onClick={() => setSkipOpen((o) => !o)}
          >
            {skipOpen ? "▾" : "▸"} Skip interview — blank project
          </button>
          {skipOpen && (
            <form className="skip-form" onSubmit={skipProvision}>
              <label className="sr-only" htmlFor="skip-name-picker">
                Project name
              </label>
              <input
                id="skip-name-picker"
                value={skipName}
                onChange={(e) => setSkipName(e.target.value)}
                placeholder="Project name…"
                disabled={skipBusy}
                style={{ minHeight: "var(--tap-min)" }}
              />
              <label className="sr-only" htmlFor="skip-desc-picker">
                Description (optional)
              </label>
              <input
                id="skip-desc-picker"
                value={skipDesc}
                onChange={(e) => setSkipDesc(e.target.value)}
                placeholder="Description (optional)"
                disabled={skipBusy}
                style={{ minHeight: "var(--tap-min)" }}
              />
              <button className="tap-target btn-secondary" disabled={skipBusy || !skipName.trim()}>
                {skipBusy ? "Creating…" : "Create blank project"}
              </button>
            </form>
          )}
        </div>
      </div>
    );
  }

  return (
    <div className="intake">
      <div
        className="intake-header"
        style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-3)" }}
      >
        {handleCancel && (
          <button className="intake-back-btn tap-target btn-secondary" onClick={handleCancel}>
            ← Back
          </button>
        )}
        <h2 className="intake-title">New Project</h2>
      </div>
      {error && <p className="err">{error}</p>}
      {countdown !== null && (
        <div
          className="intake-countdown-banner"
          style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: "var(--space-3)" }}
        >
          Creating your project and queuing jobs… {countdown}s
          <button type="button" className="tap-target btn-secondary" onClick={cancelCountdown}>
            Cancel
          </button>
        </div>
      )}
      <div
        className="intake-stepper"
        style={{ display: "flex", flexWrap: "wrap", gap: "var(--space-2)" }}
      >
        {INTAKE_PHASES.map((phase) => {
          const phaseIndex = INTAKE_PHASES.indexOf(phase);
          const activeIndex = currentPhase ? INTAKE_PHASES.indexOf(currentPhase) : -1;
          const cls =
            activeIndex >= 0 && phaseIndex < activeIndex
              ? "step-done"
              : phase === currentPhase
                ? "step-active"
                : "step-remaining";
          return (
            <span key={phase} className={`intake-step ${cls}`}>
              {cls === "step-done" ? "✓ " : ""}
              {phase}
            </span>
          );
        })}
      </div>
      {receipt ? (
        <div className="intake-receipt">
          <h3 className="section-title">{receipt.project.name}</h3>
          {(receipt.plan?.epics || []).map((epic) => {
            const epicJobs = (receipt.plan?.jobs || []).filter((j) => j.epic === epic.name);
            return (
              <div key={epic.name}>
                <h4 className="section-title">{epic.name}</h4>
                <ul>
                  {epicJobs.map((job, i) => (
                    <li key={i}>
                      {job.title}
                      <span className="priority-badge"> (priority {job.priority})</span>
                      {job.is_foundation && <span> (foundation)</span>}
                    </li>
                  ))}
                </ul>
              </div>
            );
          })}
          <button
            className="goto-board-btn tap-target btn-secondary"
            onClick={() => onNavWorkspace(receipt.project.id, "dashboard")}
          >
            Go to project board →
          </button>
        </div>
      ) : confirming ? (
        <div className="intake-creating">Creating project…</div>
      ) : (
        <div
          className="intake-body"
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fit, minmax(320px, 1fr))",
            gap: "var(--space-5)",
          }}
        >
          <div className="intake-chat">
            <div className="messages">
              {messages.length === 0 && !loading && (
                <p className="hint">Starting your intake session…</p>
              )}
              {messages.map((m, i) => (
                <div key={i} className={`msg ${m.role === "user" ? "you" : "hyqs"}`}>
                  <b>{m.role === "user" ? "you" : "hyqs"}</b>
                  {m.role === "assistant" && m.blocks ? (
                    <IntakeBlocks blocks={m.blocks} />
                  ) : (
                    <span style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}>
                      {m.text}
                    </span>
                  )}
                </div>
              ))}
              {streaming && (
                <div className="msg hyqs">
                  <b>hyqs</b>
                  <ThinkingIndicator visible={!aiText && !reconnecting} />
                  {reconnecting && <span className="reconnecting-indicator">Reconnecting…</span>}
                  {aiText && (
                    <span style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}>
                      {aiText}
                    </span>
                  )}
                  <ActivityFeed events={toolEvents} collapsible />
                  <TokenTally usage={usage} />
                </div>
              )}
              {!streaming && aiError && (
                <div className="stream-error">
                  <span className="err">{aiError.message}</span>
                  <button className="retry-btn tap-target btn-secondary" onClick={retry}>
                    Retry
                  </button>
                </div>
              )}
              <div ref={endRef} />
            </div>
            <form onSubmit={send} className="intake-form">
              <label className="sr-only" htmlFor="intake-message">
                Message
              </label>
              <input
                id="intake-message"
                value={inputText}
                onChange={(e) => setInputText(e.target.value)}
                placeholder="Tell me about your project…"
                disabled={loading || !sessionId}
                style={{ minHeight: "var(--tap-min)" }}
              />
              <button
                className="tap-target btn-secondary"
                disabled={loading || !sessionId || !inputText.trim()}
              >
                Send
              </button>
              {streaming && (
                <button type="button" className="tap-target btn-secondary" onClick={stop}>
                  Stop
                </button>
              )}
            </form>
            <div className="skip-interview-section">
              <button
                type="button"
                className="skip-interview-toggle tap-target btn-secondary"
                onClick={() => setSkipOpen((o) => !o)}
              >
                {skipOpen ? "▾" : "▸"} Skip interview — blank project
              </button>
              {skipOpen && (
                <form className="skip-form" onSubmit={skipProvision}>
                  <label className="sr-only" htmlFor="skip-name-main">
                    Project name
                  </label>
                  <input
                    id="skip-name-main"
                    value={skipName}
                    onChange={(e) => setSkipName(e.target.value)}
                    placeholder="Project name…"
                    disabled={skipBusy}
                    style={{ minHeight: "var(--tap-min)" }}
                  />
                  <label className="sr-only" htmlFor="skip-desc-main">
                    Description (optional)
                  </label>
                  <input
                    id="skip-desc-main"
                    value={skipDesc}
                    onChange={(e) => setSkipDesc(e.target.value)}
                    placeholder="Description (optional)"
                    disabled={skipBusy}
                    style={{ minHeight: "var(--tap-min)" }}
                  />
                  <button
                    className="tap-target btn-secondary"
                    disabled={skipBusy || !skipName.trim()}
                  >
                    {skipBusy ? "Creating…" : "Create blank project"}
                  </button>
                </form>
              )}
            </div>
          </div>
          <div className="intake-spec">
            <div className="intake-requirements">
              <span className="hint">
                {missing.length > 0
                  ? `Still needed: ${missing.join(", ")}`
                  : "All required fields set"}
              </span>
              <button
                type="button"
                className="create-project-btn tap-target btn-secondary"
                disabled={missing.length > 0 || confirming}
                onClick={manualConfirm}
              >
                {confirming ? "Creating…" : "Create project"}
              </button>
              {confirmError && <span className="err">{confirmError}</span>}
            </div>
            <div
              className="intake-spec-head clickable tap-target"
              style={{
                display: "flex",
                flexWrap: "wrap",
                alignItems: "center",
                gap: "var(--space-2)",
                padding: "var(--space-2) 0",
              }}
              onClick={() => setSpecOpen((o) => !o)}
            >
              <span>Project Spec</span>
              <span className="badge">
                {specFilled}/{INTAKE_REQUIRED.length}
              </span>
              <span className="expand">{specOpen ? "▾" : "▸"}</span>
            </div>
            {specOpen && (
              <div className="intake-spec-body table-scroll">
                {SCALAR_FIELDS.map((f) => {
                  const val = draftSpec[f];
                  const done = val !== undefined && val !== null && val !== "";
                  const isEditing = editingField === f;
                  const status = fieldStatus[f];
                  return (
                    <div
                      key={f}
                      className={`spec-field ${done ? "ok" : "wait"}`}
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        alignItems: "center",
                        gap: "var(--space-2)",
                        padding: "var(--space-2) 0",
                      }}
                    >
                      <span>{done ? "✓" : "—"}</span>
                      <span className="spec-field-name">{f}</span>
                      {isEditing ? (
                        <>
                          <label className="sr-only" htmlFor={`spec-field-${f}`}>
                            {f}
                          </label>
                          <input
                            id={`spec-field-${f}`}
                            className="spec-field-input"
                            value={fieldDraft}
                            onChange={(e) => setFieldDraft(e.target.value)}
                            onBlur={commitField}
                            onKeyDown={onFieldKeyDown}
                            disabled={streaming}
                            autoFocus
                            style={{ minHeight: "var(--tap-min)" }}
                          />
                        </>
                      ) : (
                        <span
                          className="spec-field-val editable tap-target"
                          style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                          onClick={() => !streaming && startEditField(f)}
                        >
                          {done ? String(val).slice(0, 60) : "(click to set)"}
                        </span>
                      )}
                      {status === "saved" && <span className="field-saved">Saved</span>}
                      {status?.error && <span className="field-error">{status.error}</span>}
                    </div>
                  );
                })}
                <div className="spec-features">
                  <div
                    className="spec-features-head"
                    style={{
                      display: "flex",
                      flexWrap: "wrap",
                      alignItems: "center",
                      gap: "var(--space-2)",
                      padding: "var(--space-2) 0",
                    }}
                  >
                    <span className="spec-field-name">features</span>
                    <span className="badge">{(draftSpec.features || []).length}</span>
                    {!streaming && editingFeatureIdx === null && (
                      <button
                        className="add-feature-btn tap-target btn-secondary"
                        onClick={startAddFeature}
                      >
                        + Add
                      </button>
                    )}
                  </div>
                  {(draftSpec.features || []).map((feat, idx) => (
                    <div
                      key={idx}
                      className={`spec-feature-row${feat.source === "suggested" ? " feat-suggested" : ""}`}
                      style={{
                        display: "flex",
                        flexWrap: "wrap",
                        alignItems: "center",
                        gap: "var(--space-2)",
                        padding: "var(--space-2) 0",
                      }}
                    >
                      {editingFeatureIdx === idx ? (
                        <FeatureForm
                          draft={featureDraft}
                          onChange={setFeatureDraft}
                          onSave={commitFeature}
                          onCancel={() => setEditingFeatureIdx(null)}
                          error={featSaveError}
                          disabled={streaming}
                        />
                      ) : feat.source === "suggested" ? (
                        <>
                          <span
                            className="feat-title"
                            style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                          >
                            {feat.title || "(untitled)"}
                          </span>
                          <span className="feat-badge">Suggested</span>
                          <span className="feat-theme">{feat.theme}</span>
                          <button
                            className="feat-accept-btn tap-target btn-secondary"
                            disabled={streaming}
                            onClick={() => acceptFeature(idx)}
                          >
                            Accept
                          </button>
                          <button
                            className="feat-reject-btn tap-target btn-ghost-danger"
                            disabled={streaming}
                            onClick={() => removeFeature(idx)}
                          >
                            Reject
                          </button>
                        </>
                      ) : (
                        <>
                          <span
                            className="feat-title"
                            style={{ overflowWrap: "anywhere", wordBreak: "break-word" }}
                          >
                            {feat.title || "(untitled)"}
                          </span>
                          <span className="feat-theme">{feat.theme}</span>
                          <button
                            className="feat-edit-btn tap-target btn-secondary"
                            disabled={streaming}
                            onClick={() => startEditFeature(idx)}
                          >
                            Edit
                          </button>
                          <button
                            className="feat-remove-btn tap-target btn-ghost-danger"
                            disabled={streaming}
                            onClick={() => removeFeature(idx)}
                          >
                            Remove
                          </button>
                        </>
                      )}
                    </div>
                  ))}
                  {editingFeatureIdx === "new" && (
                    <FeatureForm
                      draft={featureDraft}
                      onChange={setFeatureDraft}
                      onSave={commitFeature}
                      onCancel={() => setEditingFeatureIdx(null)}
                      error={featSaveError}
                      disabled={streaming}
                    />
                  )}
                  {featSaveError && editingFeatureIdx === null && (
                    <span className="field-error">{featSaveError}</span>
                  )}
                </div>
              </div>
            )}
          </div>
        </div>
      )}
      {showJump && (
        <button
          className="jump-to-latest tap-target btn-secondary"
          onClick={() => {
            userScrolledUp.current = false;
            setShowJump(false);
            endRef.current?.scrollIntoView({ behavior: "smooth" });
          }}
        >
          Jump to latest ↓
        </button>
      )}
    </div>
  );
}
