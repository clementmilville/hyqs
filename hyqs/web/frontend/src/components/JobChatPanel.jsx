import { useCallback, useEffect, useRef, useState } from "react";
import { streamJobChat, createBatchJobs, suggestEpicForJob, getChatSession } from "../api.js";
import { GatedAction } from "../context.js";
import { ThinkingIndicator } from "./ThinkingIndicator.jsx";
import { useToast } from "./Toast.jsx";
import { MarkdownContent } from "./MarkdownContent.jsx";
import { ActivityFeed } from "./ActivityFeed.jsx";
import { IdeationPanel } from "./IdeationPanel.jsx";

const ALLOWED_MIME = new Set(["image/jpeg", "image/png", "image/gif", "image/webp"]);
const MAX_IMAGE_BYTES = 5 * 1024 * 1024;
const NEW_EPIC_SENTINEL = "__new__";

// Topological sort: returns array of levels, each level is an array of job indices.
function buildRunLevels(jobs) {
  const n = jobs.length;
  const adj = Array.from({ length: n }, () => []);
  const indegree = new Array(n).fill(0);
  jobs.forEach((job, i) => {
    (job.depends_on ?? []).forEach((dep) => {
      if (dep >= 0 && dep < n) {
        adj[dep].push(i);
        indegree[i]++;
      }
    });
  });
  const levels = [];
  let current = Array.from({ length: n }, (_, i) => i).filter((i) => indegree[i] === 0);
  while (current.length > 0) {
    levels.push(current);
    const next = [];
    current.forEach((i) => {
      adj[i].forEach((j) => {
        if (--indegree[j] === 0) next.push(j);
      });
    });
    current = next;
  }
  return levels;
}

export function JobChatPanel({ projectId, projectRepo, epicId, epics, onJobCreated }) {
  const storageKey = `jobchat:${projectId}:${epicId ?? "none"}`;
  const activeEpics = (epics ?? []).filter((ep) => !ep.archived);

  const initialSessionId = (() => {
    try {
      return new URLSearchParams(window.location.search).get("session") || null;
    } catch {
      return null;
    }
  })();

  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [proposedJobs, setProposedJobs] = useState([]);
  const [streaming, setStreaming] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [stagedImage, setStagedImage] = useState(null);
  const [toolEvents, setToolEvents] = useState([]);
  const [sessionId, setSessionId] = useState(initialSessionId);
  const cancelRef = useRef(null);
  const threadRef = useRef(null);
  const confirmBtnRef = useRef(null);
  const fileInputRef = useRef(null);
  const inFlight = useRef(false);
  const suggestedTitlesRef = useRef(new Set());
  const toast = useToast();

  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [messages]);

  const prevProposedLengthRef = useRef(0);
  useEffect(() => {
    if (proposedJobs.length > prevProposedLengthRef.current) {
      confirmBtnRef.current?.scrollIntoView?.({ behavior: "smooth", block: "nearest" });
    }
    prevProposedLengthRef.current = proposedJobs.length;
  }, [proposedJobs.length]);

  useEffect(
    () => () => {
      if (cancelRef.current) cancelRef.current();
    },
    []
  );

  // Restore state on mount: prefer backend session if ?session= param present, else sessionStorage
  useEffect(() => {
    if (initialSessionId) {
      getChatSession(initialSessionId)
        .then((sess) => {
          const msgs = (sess.messages ?? []).filter((m) => !m.pending);
          if (msgs.length) setMessages(msgs);
          if (sess.proposed_jobs?.length) setProposedJobs(sess.proposed_jobs);
        })
        .catch(() => {
          // Fall back to sessionStorage if backend fetch fails
          try {
            const raw = sessionStorage.getItem(storageKey);
            if (!raw) return;
            const saved = JSON.parse(raw);
            const msgs = (saved.messages ?? []).filter((m) => !m.pending);
            if (msgs.length) setMessages(msgs);
            if (saved.proposedJobs?.length) setProposedJobs(saved.proposedJobs);
            if (saved.draft) setDraft(saved.draft);
          } catch {
            // ignore
          }
        });
    } else {
      try {
        const raw = sessionStorage.getItem(storageKey);
        if (!raw) return;
        const saved = JSON.parse(raw);
        const msgs = (saved.messages ?? []).filter((m) => !m.pending);
        if (msgs.length) setMessages(msgs);
        if (saved.proposedJobs?.length) setProposedJobs(saved.proposedJobs);
        if (saved.draft) setDraft(saved.draft);
      } catch {
        // ignore parse errors
      }
    }
  }, []); // intentional: run only on mount to restore persisted state once

  // Persist state to sessionStorage on every change
  useEffect(() => {
    if (!messages.length) {
      sessionStorage.removeItem(storageKey);
      return;
    }
    sessionStorage.setItem(
      storageKey,
      JSON.stringify({
        messages: messages.filter((m) => !m.pending),
        proposedJobs,
        draft,
      })
    );
  }, [messages, proposedJobs, draft, storageKey]);

  // Debounced epic suggest per proposed job (fires once per distinct title)
  useEffect(() => {
    if (!projectId) return;
    proposedJobs.forEach((job) => {
      if (job.epicId !== "" || suggestedTitlesRef.current.has(job.title)) return;
      suggestedTitlesRef.current.add(job.title);
      setTimeout(async () => {
        const suggestion = await suggestEpicForJob(
          projectId,
          job.title + " " + (job.description ?? "")
        );
        setProposedJobs((prev) => {
          const idx = prev.findIndex((j) => j.title === job.title);
          if (idx === -1) return prev;
          const copy = [...prev];
          if (suggestion?.epic_id) {
            copy[idx] = { ...copy[idx], epicId: String(suggestion.epic_id) };
          } else if (suggestion?.proposed_name) {
            copy[idx] = {
              ...copy[idx],
              epicId: NEW_EPIC_SENTINEL,
              newEpicName: suggestion.proposed_name,
            };
          }
          return copy;
        });
      }, 300);
    });
  }, [proposedJobs, projectId]);

  const setProposedJob = useCallback((idx, patch) => {
    setProposedJobs((prev) => {
      const copy = [...prev];
      copy[idx] = { ...copy[idx], ...patch };
      return copy;
    });
  }, []);

  const handleFileChange = useCallback(
    (e) => {
      const file = e.target.files[0];
      if (!file) return;
      e.target.value = "";
      if (!ALLOWED_MIME.has(file.type)) {
        toast.error("Only JPEG, PNG, GIF, and WebP images are supported.");
        return;
      }
      if (file.size > MAX_IMAGE_BYTES) {
        toast.error("Image must be 5 MB or smaller.");
        return;
      }
      const reader = new FileReader();
      reader.onload = (ev) => {
        const dataUrl = ev.target.result;
        const b64 = dataUrl.split(",")[1];
        setStagedImage({ dataUrl, b64, mediaType: file.type });
      };
      reader.readAsDataURL(file);
    },
    [toast]
  );

  const send = useCallback(() => {
    const text = draft.trim();
    if (!text || streaming) return;

    setDraft("");

    const content = stagedImage
      ? [
          {
            type: "image",
            source: { type: "base64", media_type: stagedImage.mediaType, data: stagedImage.b64 },
          },
          { type: "text", text },
        ]
      : text;

    setStagedImage(null);

    const apiMessages = [...messages.filter((m) => !m.pending), { role: "user", content }];

    setMessages((prev) => [
      ...prev.filter((m) => !m.pending),
      { role: "user", content },
      { role: "assistant", content: "", pending: true },
    ]);
    setToolEvents([]);
    setStreaming(true);

    const { cancel } = streamJobChat(
      projectId,
      apiMessages,
      epicId,
      {
        onToolUse: (ev) => setToolEvents((prev) => [...prev, ev]),
        onText: (delta) => {
          if (delta === "") {
            setMessages((prev) => {
              const copy = [...prev];
              const last = copy[copy.length - 1];
              if (last?.pending) copy[copy.length - 1] = { ...last, content: "" };
              return copy;
            });
          } else {
            setMessages((prev) => {
              const copy = [...prev];
              const last = copy[copy.length - 1];
              if (last?.pending)
                copy[copy.length - 1] = { ...last, content: last.content + delta };
              return copy;
            });
          }
        },
        onSession: (ev) => {
          const sid = ev.session_id;
          setSessionId(sid);
          try {
            const url = new URL(window.location.href);
            url.searchParams.set("session", sid);
            window.history.replaceState(null, "", url.toString());
          } catch {
            // ignore
          }
        },
        onResult: (ev) => {
          cancelRef.current = null;
          setStreaming(false);
          setMessages((prev) => {
            const copy = [...prev];
            const last = copy[copy.length - 1];
            if (last?.pending)
              copy[copy.length - 1] = { role: "assistant", content: last.content };
            return copy;
          });
          if (ev.jobs && ev.jobs.length > 0) {
            setProposedJobs((prev) => {
              const existingTitles = new Set(prev.map((j) => j.title.trim().toLowerCase()));
              const fresh = ev.jobs
                .filter((j) => !existingTitles.has(j.title.trim().toLowerCase()))
                .map((job) => ({
                  ...job,
                  epicId: epicId != null ? String(epicId) : "",
                  newEpicName: "",
                  newEpicDesc: "",
                  altPanel:
                    job.alternatives?.length > 0
                      ? {
                          options: job.alternatives.map((a, i) => ({
                            id: i,
                            title: a.title,
                            description: a.description,
                            acceptance: a.acceptance_criteria ?? "",
                          })),
                          selectedIdx: null,
                          editedTitle: "",
                          editedDesc: "",
                          refineNote: "",
                        }
                      : null,
                }));
              return fresh.length ? [...prev, ...fresh] : prev;
            });
          }
        },
        onError: (err) => {
          cancelRef.current = null;
          setStreaming(false);
          toast.error(err.message);
          setMessages((prev) => {
            const copy = [...prev];
            if (copy[copy.length - 1]?.pending) copy.pop();
            return copy;
          });
        },
      },
      sessionId
    );
    cancelRef.current = cancel;
  }, [draft, streaming, messages, projectId, epicId, toast, stagedImage, sessionId]);

  const handleKeyDown = useCallback(
    (e) => {
      if (e.key === "Enter" && !e.shiftKey) {
        e.preventDefault();
        send();
      }
    },
    [send]
  );

  const removeProposal = useCallback((idx) => {
    setProposedJobs((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const anyUnresolvedEpic =
    activeEpics.length > 0 &&
    proposedJobs.some(
      (j) => j.epicId === "" || (j.epicId === NEW_EPIC_SENTINEL && !j.newEpicName?.trim())
    );

  const confirmAll = useCallback(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    if (!proposedJobs.length || confirming) {
      inFlight.current = false;
      return;
    }
    setConfirming(true);
    try {
      await createBatchJobs(
        projectRepo,
        proposedJobs.map((j) => ({
          title: j.title,
          description: j.description ?? "",
          acceptance_criteria: j.acceptance_criteria ?? "",
          epic_id: j.epicId === "" || j.epicId === NEW_EPIC_SENTINEL ? null : +j.epicId,
          new_epic:
            j.epicId === NEW_EPIC_SENTINEL && j.newEpicName?.trim()
              ? { name: j.newEpicName.trim(), description: j.newEpicDesc?.trim() ?? "" }
              : null,
          depends_on: j.depends_on ?? [],
        })),
        sessionId
      );
      onJobCreated?.();
      toast.success(`${proposedJobs.length} job${proposedJobs.length === 1 ? "" : "s"} queued`);
      setProposedJobs([]);
      setMessages([]);
      setSessionId(null);
      sessionStorage.removeItem(storageKey);
      try {
        const url = new URL(window.location.href);
        url.searchParams.delete("session");
        window.history.replaceState(null, "", url.toString());
      } catch {
        // ignore
      }
    } catch (err) {
      toast.error(err.message);
    } finally {
      setConfirming(false);
      inFlight.current = false;
    }
  }, [proposedJobs, confirming, projectRepo, onJobCreated, toast, storageKey, sessionId]);

  const hasDeps = proposedJobs.some((j) => (j.depends_on ?? []).length > 0);

  return (
    <div className="job-chat-panel">
      <div className="job-chat-thread" ref={threadRef}>
        {messages.length === 0 && (
          <p className="dim job-chat-hint">
            Describe what you want to build. Claude will help you plan concrete jobs.
          </p>
        )}
        {messages.map((msg, i) => (
          <div
            key={i}
            className={`job-chat-bubble ${msg.role === "user" ? "job-chat-user" : "job-chat-assistant"}`}
          >
            {msg.pending ? (
              <>
                {msg.content ? (
                  <MarkdownContent content={msg.content} />
                ) : (
                  <ThinkingIndicator visible />
                )}
                <ActivityFeed events={toolEvents} collapsible={false} />
              </>
            ) : msg.role === "user" ? (
              Array.isArray(msg.content) ? (
                <>
                  {msg.content.map((block, bi) =>
                    block.type === "image" ? (
                      <img
                        key={bi}
                        className="job-chat-inline-img"
                        src={`data:${block.source.media_type};base64,${block.source.data}`}
                        alt=""
                      />
                    ) : (
                      <span key={bi} className="job-chat-text">
                        {block.text}
                      </span>
                    )
                  )}
                </>
              ) : (
                <span className="job-chat-text">{msg.content}</span>
              )
            ) : (
              <MarkdownContent content={msg.content} />
            )}
          </div>
        ))}
      </div>

      {proposedJobs.length > 0 && (
        <div className="job-chat-proposals">
          <div className="job-chat-proposals-head">Proposed jobs</div>
          {proposedJobs.map((job, jobIdx) => (
            <div key={jobIdx} className="job-chat-card">
              <div className="job-chat-card-head">
                <span className="job-chat-card-title">{job.title}</span>
                <button
                  className="job-chat-remove"
                  onClick={() => removeProposal(jobIdx)}
                  aria-label="Remove proposal"
                >
                  ✕
                </button>
              </div>
              {job.description && <p className="job-chat-card-desc">{job.description}</p>}
              {job.acceptance_criteria && (
                <details className="job-chat-card-acceptance">
                  <summary>Acceptance criteria</summary>
                  <p>{job.acceptance_criteria}</p>
                </details>
              )}

              {job.altPanel && job.altPanel.loading ? (
                <ThinkingIndicator visible />
              ) : job.altPanel ? (
                <IdeationPanel
                  panel={job.altPanel}
                  confirmDisabled={false}
                  onSelect={(i) => {
                    const opt = job.altPanel.options[i];
                    setProposedJob(jobIdx, {
                      altPanel: {
                        ...job.altPanel,
                        selectedIdx: i,
                        editedTitle: opt.title,
                        editedDesc: opt.description,
                      },
                    });
                  }}
                  onEditTitle={(v) =>
                    setProposedJob(jobIdx, { altPanel: { ...job.altPanel, editedTitle: v } })
                  }
                  onEditDesc={(v) =>
                    setProposedJob(jobIdx, { altPanel: { ...job.altPanel, editedDesc: v } })
                  }
                  onRefineChange={(v) =>
                    setProposedJob(jobIdx, { altPanel: { ...job.altPanel, refineNote: v } })
                  }
                  onRegenerate={() => {}}
                  onConfirm={() =>
                    setProposedJob(jobIdx, {
                      title: job.altPanel.editedTitle,
                      description: job.altPanel.editedDesc,
                      altPanel: null,
                    })
                  }
                  onCancel={() => setProposedJob(jobIdx, { altPanel: null })}
                />
              ) : null}

              <div className="job-chat-epic-row">
                <label className="job-chat-epic-label">Epic</label>
                <select
                  className="job-chat-epic-select"
                  value={job.epicId ?? ""}
                  onChange={(e) => {
                    const val = e.target.value;
                    setProposedJob(jobIdx, {
                      epicId: val,
                      newEpicName: val !== NEW_EPIC_SENTINEL ? "" : job.newEpicName,
                      newEpicDesc: val !== NEW_EPIC_SENTINEL ? "" : job.newEpicDesc,
                    });
                  }}
                >
                  <option value="">Auto-assign</option>
                  {activeEpics.map((ep) => (
                    <option key={ep.id} value={String(ep.id)}>
                      {ep.name}
                    </option>
                  ))}
                  <option value={NEW_EPIC_SENTINEL}>Create new epic…</option>
                </select>
              </div>
              {job.epicId === NEW_EPIC_SENTINEL && (
                <div className="job-chat-new-epic-fields">
                  <input
                    className="job-chat-new-epic-name"
                    value={job.newEpicName ?? ""}
                    onChange={(e) => setProposedJob(jobIdx, { newEpicName: e.target.value })}
                    placeholder="New epic name (required)"
                  />
                  <input
                    className="job-chat-new-epic-desc"
                    value={job.newEpicDesc ?? ""}
                    onChange={(e) => setProposedJob(jobIdx, { newEpicDesc: e.target.value })}
                    placeholder="Description (optional)"
                  />
                </div>
              )}
            </div>
          ))}

          {hasDeps &&
            (() => {
              const levels = buildRunLevels(proposedJobs);
              return (
                <div className="job-chat-run-order">
                  <div className="job-chat-run-order-label">Run order</div>
                  <div className="job-chat-run-order-levels">
                    {levels.map((levelIdxs, li) => (
                      <span key={li}>
                        {li > 0 && <span className="job-chat-run-order-arrow"> → </span>}
                        <span className="job-chat-run-order-level">
                          {levelIdxs.map((i) => proposedJobs[i].title).join(", ")}
                        </span>
                      </span>
                    ))}
                  </div>
                </div>
              );
            })()}
        </div>
      )}

      {stagedImage && (
        <div className="job-chat-image-preview">
          <img className="job-chat-preview-thumb" src={stagedImage.dataUrl} alt="staged" />
          <button
            className="job-chat-preview-remove"
            onClick={() => setStagedImage(null)}
            aria-label="Remove image"
          >
            ✕
          </button>
        </div>
      )}

      <input
        type="file"
        ref={fileInputRef}
        accept="image/jpeg,image/png,image/gif,image/webp"
        style={{ display: "none" }}
        onChange={handleFileChange}
      />

      <div className="job-chat-compose">
        <textarea
          className="job-chat-textarea"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Describe what to build… (Enter to send, Shift+Enter for newline)"
          rows={2}
          disabled={streaming}
        />
        <button
          className="job-chat-attach"
          onClick={() => fileInputRef.current?.click()}
          disabled={streaming}
          aria-label="Attach image"
          title="Attach image"
        >
          📎
        </button>
        <GatedAction require="queue_job">
          <button className="job-chat-send" onClick={send} disabled={!draft.trim() || streaming}>
            {streaming ? "…" : "Send"}
          </button>
        </GatedAction>
      </div>

      {proposedJobs.length > 0 && (
        <GatedAction require="queue_job">
          <button
            ref={confirmBtnRef}
            className="job-chat-confirm"
            onClick={confirmAll}
            disabled={confirming || anyUnresolvedEpic}
          >
            {confirming
              ? "Creating…"
              : `Create ${proposedJobs.length} job${proposedJobs.length === 1 ? "" : "s"}`}
          </button>
        </GatedAction>
      )}
    </div>
  );
}
