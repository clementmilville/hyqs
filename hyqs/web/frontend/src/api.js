// Thin API client. Token is stored in localStorage and sent as a bearer
// header (and as ?token= for SSE, which can't set headers).

const TOKEN_KEY = "hyqs_token";

export const getToken = () => localStorage.getItem(TOKEN_KEY) || "";
export const setToken = (t) => localStorage.setItem(TOKEN_KEY, t);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

// Check a candidate token against an authenticated endpoint without saving it.
// Returns true only if the server accepts it (not a 401).
export async function verifyToken(token) {
  try {
    const res = await fetch("/api/me", {
      headers: { Authorization: `Bearer ${token}` },
    });
    return res.status !== 401;
  } catch {
    return false;
  }
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    ...opts,
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...(opts.headers || {}),
    },
  });
  if (res.status === 401) {
    // The stored token is no longer valid — drop it and kick back to the gate
    // rather than leaving the UI silently failing every request.
    clearToken();
    window.dispatchEvent(new Event("hyqs-unauthorized"));
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    const err = new Error(body.error || `HTTP ${res.status}`);
    err.status = res.status;
    err.body = body;
    throw err;
  }
  return res.json();
}

export async function logout() {
  try {
    await api("/api/auth/logout", { method: "POST" });
  } catch (_) {}
}

export const getMe = () => api("/api/me");

export const createJob = (idea, repo, epicId, dependsOn, title, newEpic) =>
  api("/api/jobs", {
    method: "POST",
    body: JSON.stringify({
      idea,
      repo,
      epic_id: epicId ?? null,
      new_epic: newEpic ?? null,
      depends_on: dependsOn ?? null,
      title: title || undefined,
    }),
  });

export const createBatchJobs = (repo, jobs, sessionId) =>
  api("/api/jobs/batch", {
    method: "POST",
    body: JSON.stringify({ repo, jobs, ...(sessionId ? { session_id: sessionId } : {}) }),
  });

export const suggestEpicForJob = (projectId, idea) =>
  api("/api/epics/suggest", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, idea }),
  }).catch(() => ({}));

export const patchJobTitle = (id, title) =>
  api(`/api/jobs/${id}/title`, { method: "PATCH", body: JSON.stringify({ title }) });

export const getUsage = (since, projectId) => {
  const p = new URLSearchParams();
  if (since) p.set("since", since);
  if (projectId != null) p.set("project_id", String(projectId));
  const s = p.toString();
  return api(s ? `/api/usage?${s}` : "/api/usage");
};

export const getJobsFiledByBreakdown = (since) => {
  const p = new URLSearchParams();
  if (since) p.set("since", since);
  const s = p.toString();
  return api(s ? `/api/jobs/filed-by-breakdown?${s}` : "/api/jobs/filed-by-breakdown").then(
    (d) => d.breakdown
  );
};

export const getDeploymentReliability = (since) => {
  const p = new URLSearchParams();
  if (since) p.set("since", since);
  const s = p.toString();
  return api(s ? `/api/deployment-reliability?${s}` : "/api/deployment-reliability").then(
    (d) => d.breakdown
  );
};

export const getPageViewsSummary = ({ since, until, path } = {}) => {
  const params = new URLSearchParams();
  if (since) params.set("since", since);
  if (until) params.set("until", until);
  if (path) params.set("path", path);
  const qs = params.toString();
  return api(`/api/admin/page-views${qs ? `?${qs}` : ""}`);
};

export async function cancelJob(id) {
  const res = await fetch(`/api/jobs/${id}`, {
    method: "DELETE",
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (res.status === 401) {
    clearToken();
    window.dispatchEvent(new Event("hyqs-unauthorized"));
    throw new Error("unauthorized");
  }
  if (!res.ok) {
    const err = new Error((await res.json().catch(() => ({}))).error || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
}

export const retryJob = (id, { force, overrideActiveRemediation } = {}) => {
  const body = {};
  if (force) body.force = true;
  if (overrideActiveRemediation) body.override_active_remediation = true;
  return api(`/api/jobs/${id}/retry`, {
    method: "POST",
    ...(Object.keys(body).length ? { body: JSON.stringify(body) } : {}),
  });
};
export const setJobPriority = (id, priority) =>
  api(`/api/jobs/${id}/priority`, { method: "POST", body: JSON.stringify({ priority }) });
export const archiveJob = (id) => api(`/api/jobs/${id}/archive`, { method: "POST" });
export const patchJobEpic = (id, epicId) =>
  api(`/api/jobs/${id}/epic`, { method: "PATCH", body: JSON.stringify({ epic_id: epicId }) });
export const unarchiveJob = (id) => api(`/api/jobs/${id}/unarchive`, { method: "POST" });
export const listJobsFiltered = (status, projectId) => {
  const params = new URLSearchParams({ status });
  if (projectId != null) params.set("project_id", String(projectId));
  return api(`/api/jobs?${params}`).then((d) => d.jobs);
};

export const getJob = (id) => api(`/api/jobs/${id}`).then((d) => d.job);
export const getJobEvents = (id) => api(`/api/jobs/${id}/events`).then((d) => d.events);
export const getJobSupervisorEvents = (id) =>
  api(`/api/jobs/${id}/events`).then((d) => d.supervisor_events);
export const requeueJobAtStage = (id, stage) =>
  api(`/api/jobs/${id}/requeue`, { method: "POST", body: JSON.stringify({ stage }) });
export const fileFixForwardJob = (
  id,
  idea,
  title,
  repointDependentIds,
  overrideActiveRemediation
) =>
  api(`/api/jobs/${id}/fix-forward`, {
    method: "POST",
    body: JSON.stringify({
      idea,
      ...(title ? { title } : {}),
      ...(repointDependentIds && repointDependentIds.length
        ? { repoint_dependent_ids: repointDependentIds }
        : {}),
      ...(overrideActiveRemediation ? { override_active_remediation: true } : {}),
    }),
  });
export const patchJobIdea = (id, idea) =>
  api(`/api/jobs/${id}/idea`, { method: "PATCH", body: JSON.stringify({ idea }) });
export const resolveJob = (id) => api(`/api/jobs/${id}/resolve`, { method: "POST" });
export const getJobDiff = (id) => api(`/api/jobs/${id}/diff`);
export const getJobResources = (id) => api(`/api/jobs/${id}/resources`);
export const getJobUsage = (id) => api(`/api/jobs/${id}/usage`);
export const getJobDependencies = (id) =>
  api(`/api/jobs/${id}/dependencies`).then((d) => d.depends_on);
export const getJobDependents = (id) => api(`/api/jobs/${id}/dependents`).then((d) => d.dependents);
export const getJobDependencyJobs = (id) =>
  getJobDependencies(id).then((ids) => Promise.all(ids.map((depId) => getJob(depId))));
export const addJobDependency = (id, depId) =>
  api(`/api/jobs/${id}/dependencies`, {
    method: "POST",
    body: JSON.stringify({ depends_on_job_id: depId }),
  }).then((d) => d.depends_on);
export const removeJobDependency = (id, depId) =>
  api(`/api/jobs/${id}/dependencies/${depId}`, { method: "DELETE" }).then((d) => d.depends_on);

// --- projects & epics ---
export const listProjects = () => api("/api/projects").then((d) => d.projects);
// Provision a brand-new project: scaffolds a local repo + (private) GitHub repo.
export const provisionProject = (name, description) =>
  api("/api/projects/provision", { method: "POST", body: JSON.stringify({ name, description }) });
export const updateProject = (id, patch) =>
  api(`/api/projects/${id}`, { method: "PATCH", body: JSON.stringify(patch) }).then(
    (d) => d.project
  );
export const deleteProject = (id) => api(`/api/projects/${id}`, { method: "DELETE" });

export const getDeployStatus = (projectId) => api(`/api/projects/${projectId}/deploy-status`);
export const triggerDeploy = (projectId) =>
  api(`/api/projects/${projectId}/deploy`, { method: "POST" });
export const restartProject = (projectId) =>
  api(`/api/projects/${projectId}/restart`, { method: "POST" });
export const stopProject = (projectId) =>
  api(`/api/projects/${projectId}/stop`, {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
export const startProject = (projectId) =>
  api(`/api/projects/${projectId}/start`, { method: "POST" });

// --- runtime status (live docker/compose state) ---
export const getProjectsRuntime = () => api("/api/projects/runtime");
export const getProjectRuntime = (projectId) => api(`/api/projects/${projectId}/runtime`);
export const getProjectChangelog = (projectId, prose = false) =>
  api(`/api/projects/${projectId}/changelog${prose ? "?include_prose=true" : ""}`);

// --- on-demand log tail (no SSH) ---
export const getProjectLogs = (projectId, { service, tail } = {}) =>
  api(
    `/api/projects/${projectId}/logs?${new URLSearchParams({
      ...(service ? { service } : {}),
      ...(tail ? { tail } : {}),
    })}`
  );

// --- agent roster ---
export const getAgentMeta = () => api("/api/agent-meta");
export const listAgents = (projectId) =>
  api(`/api/projects/${projectId}/agents`).then((d) => d.agents);
export const agentStats = (projectId) =>
  api(`/api/projects/${projectId}/agent-stats`).then((d) => d.stats);
export const createAgent = (projectId, agent) =>
  api(`/api/projects/${projectId}/agents`, { method: "POST", body: JSON.stringify(agent) }).then(
    (d) => d.agent
  );
export const updateAgent = (id, patch) =>
  api(`/api/agents/${id}`, { method: "PATCH", body: JSON.stringify(patch) }).then((d) => d.agent);
export const deleteAgent = (id) => api(`/api/agents/${id}`, { method: "DELETE" });

export const listEpics = (projectId, includeArchived = false) => {
  const params = new URLSearchParams();
  if (projectId) params.set("project_id", String(projectId));
  if (includeArchived) params.set("include_archived", "true");
  const qs = params.toString();
  return api(`/api/epics${qs ? `?${qs}` : ""}`).then((d) => d.epics);
};
export const archiveEpic = (epicId) => api(`/api/epics/${epicId}/archive`, { method: "POST" });
export const unarchiveEpic = (epicId) => api(`/api/epics/${epicId}/unarchive`, { method: "POST" });
export const createEpic = (projectId, name, description) =>
  api("/api/epics", {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, name, description }),
  }).then((d) => d.epic);
// --- streaming AI helpers (SSE) ---
// Each backend event is a single SSE data line: data: <json>\n\n
// where json has a `type` field: text | tool_use | thinking | result | error
//
// Retries up to MAX_RETRIES times on network error (before a result event
// arrives), with exponential back-off capped at 8s. Signals reconnect state
// via cbs.onReconnecting and resets accumulated text via cbs.onText("").
export function streamAI(url, body, cbs) {
  const controller = new AbortController();
  const { onText, onToolUse, onThinking, onResult, onError, onReconnecting, onSession } = cbs;
  const MAX_RETRIES = 3;

  async function run() {
    for (let attempt = 0; attempt <= MAX_RETRIES; attempt++) {
      if (attempt > 0) {
        onText && onText("");
        onReconnecting && onReconnecting(attempt);
        const delay = Math.min(Math.pow(2, attempt), 8) * 1000;
        await new Promise((r) => setTimeout(r, delay));
        if (controller.signal.aborted) return;
      }

      let gotResult = false;
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            Authorization: `Bearer ${getToken()}`,
          },
          body: JSON.stringify(body),
          signal: controller.signal,
        });
        if (res.status === 401) {
          clearToken();
          window.dispatchEvent(new Event("hyqs-unauthorized"));
          onError && onError(new Error("unauthorized"));
          return;
        }
        if (!res.ok) {
          const err = await res.json().catch(() => ({}));
          onError && onError(new Error(err.error || `HTTP ${res.status}`));
          return;
        }

        const reader = res.body.getReader();
        const dec = new TextDecoder();
        let buf = "";

        while (true) {
          const { done, value } = await reader.read();
          if (done) break;
          buf += dec.decode(value, { stream: true });
          const lines = buf.split("\n");
          buf = lines.pop();
          for (const line of lines) {
            if (!line.startsWith("data:")) continue;
            const raw = line.slice(5).trim();
            if (!raw) continue;
            try {
              const ev = JSON.parse(raw);
              if (ev.type === "text") onText && onText(ev.delta || "");
              else if (ev.type === "tool_use") onToolUse && onToolUse(ev);
              else if (ev.type === "thinking") onThinking && onThinking(ev.delta || "");
              else if (ev.type === "session") onSession && onSession(ev);
              else if (ev.type === "result") {
                gotResult = true;
                onResult && onResult(ev);
              } else if (ev.type === "error") {
                onError && onError(new Error(ev.message || "stream error"));
                return;
              }
            } catch (_) {}
          }
        }

        if (gotResult) return;
        throw new Error("stream ended without a result");
      } catch (err) {
        if (err.name === "AbortError") return;
        if (gotResult) return;
        if (attempt >= MAX_RETRIES) {
          onError && onError(err);
          return;
        }
        // Will retry — loop continues
      }
    }
  }

  run();
  return { cancel: () => controller.abort() };
}

export const streamChat = (text, cbs) => streamAI("/api/chat/stream", { text }, cbs);

export const streamSuggest = (epicId, cbs) =>
  streamAI(`/api/epics/${epicId}/suggest/stream`, {}, cbs);

export const streamArchitectPlan = (epicId, goal, cbs) =>
  streamAI(`/api/epics/${epicId}/architect-plan/stream`, { goal }, cbs);

export const streamJobChat = (projectId, messages, epicId, cbs, sessionId) =>
  streamAI(
    "/api/jobs/chat/stream",
    {
      project_id: projectId,
      messages,
      epic_id: epicId ?? null,
      ...(sessionId ? { session_id: sessionId } : {}),
    },
    cbs
  );

export const getChatSession = (sessionId) => api(`/api/chat_sessions/${sessionId}`);

export function streamJobs(onJobs, onError) {
  const es = new EventSource(`/api/jobs/stream?token=${encodeURIComponent(getToken())}`);
  es.addEventListener("jobs", (e) => onJobs(JSON.parse(e.data)));
  es.onerror = () => onError && onError();
  return es;
}

export function streamLogs(jobId, afterId, onLines, onError) {
  const es = new EventSource(
    `/api/jobs/${jobId}/logs/stream?token=${encodeURIComponent(getToken())}&after_id=${afterId}`
  );
  es.addEventListener("log", (e) => onLines(JSON.parse(e.data)));
  es.onerror = () => onError && onError();
  return es;
}

export function streamJobDetail(jobId, onData, onError) {
  const es = new EventSource(`/api/jobs/${jobId}/stream?token=${encodeURIComponent(getToken())}`);
  let complete = false;
  es.addEventListener("job", (e) => {
    const snapshot = JSON.parse(e.data);
    onData(snapshot);
    if (["done", "failed", "cancelled"].includes(snapshot.job?.status)) {
      complete = true;
      es.close();
    }
  });
  es.onerror = (error) => {
    if (!complete && onError) onError(error);
  };
  return es;
}

// --- live worker fleet (C2) ---
export const getWorkers = () => api("/api/workers");
export function streamWorkers(onData, onError) {
  const es = new EventSource(`/api/workers/stream?token=${encodeURIComponent(getToken())}`);
  es.addEventListener("workers", (e) => onData(JSON.parse(e.data)));
  es.onerror = () => onError && onError();
  return es;
}

// --- supervisor tab ---
export const getSupervisor = () => api("/api/supervisor");
export function streamSupervisor(onData, onError) {
  const es = new EventSource(`/api/supervisor/stream?token=${encodeURIComponent(getToken())}`);
  es.addEventListener("supervisor", (e) => onData(JSON.parse(e.data)));
  es.onerror = () => onError && onError();
  return es;
}

// --- providers ---
export const listProviders = () => api("/api/providers").then((d) => d.providers);
export const clearProviderPause = (provider) =>
  api(`/api/providers/${encodeURIComponent(provider)}/pause`, { method: "DELETE" });

// --- admin: audit trail ---
export const getAuditLog = (filters = {}) => {
  const p = new URLSearchParams();
  if (filters.table) p.set("table", filters.table);
  if (filters.actor) p.set("actor", filters.actor);
  if (filters.row_pk) p.set("row_pk", filters.row_pk);
  if (filters.limit) p.set("limit", String(filters.limit));
  if (filters.before_id) p.set("before_id", String(filters.before_id));
  const qs = p.toString();
  return api(`/api/admin/audit${qs ? `?${qs}` : ""}`).then((d) => d.entries);
};

// --- admin: role permissions ---
export const fetchRolePermissions = () => api("/api/admin/roles/permissions");
export const patchRolePermission = (role, permission, enabled) =>
  api(`/api/admin/roles/${encodeURIComponent(role)}/permissions`, {
    method: "PATCH",
    body: JSON.stringify({ permission, enabled }),
  });

// --- performance analytics ---
function _perfQS(filters = {}) {
  const p = new URLSearchParams();
  if (filters.from) p.set("from", filters.from);
  if (filters.to) p.set("to", filters.to);
  if (filters.stage) p.set("stage", filters.stage);
  if (filters.epic_id != null) p.set("epic_id", String(filters.epic_id));
  if (filters.status) p.set("status", filters.status);
  if (filters.provider) p.set("provider", filters.provider);
  if (filters.bucket) p.set("bucket", filters.bucket);
  const s = p.toString();
  return s ? `?${s}` : "";
}

export const getPerfHeadline = (pid, filters = {}) =>
  api(`/api/projects/${pid}/performance/headline${_perfQS(filters)}`);
export const getPerfStageStats = (pid, filters = {}) =>
  api(`/api/projects/${pid}/performance/stage-stats${_perfQS(filters)}`).then((d) => d.stages);
export const getPerfSlowestJobs = (pid, filters = {}) =>
  api(`/api/projects/${pid}/performance/slowest-jobs${_perfQS(filters)}`).then((d) => d.jobs);
export const getPerfTrend = (pid, filters = {}) =>
  api(`/api/projects/${pid}/performance/trend${_perfQS(filters)}`);

// --- project members ---
export const listProjectMembers = (projectId) =>
  api(`/api/projects/${projectId}/members`).then((d) => d.members);
export const addProjectMember = (projectId, userId, role) =>
  api(`/api/projects/${projectId}/members`, {
    method: "POST",
    body: JSON.stringify({ user_id: userId, role }),
  }).then((d) => d.member);
export const removeProjectMember = (projectId, userId) =>
  api(`/api/projects/${projectId}/members/${encodeURIComponent(userId)}`, { method: "DELETE" });
export const updateMemberRole = (projectId, userId, role) =>
  api(`/api/projects/${projectId}/members/${encodeURIComponent(userId)}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  }).then((d) => d.member);

// --- project API tokens ---
export const listApiTokens = (projectId) =>
  api(`/api/projects/${projectId}/tokens`).then((d) => d.tokens);
export const createApiToken = (projectId, name, role) =>
  api(`/api/projects/${projectId}/tokens`, {
    method: "POST",
    body: JSON.stringify({ name, role }),
  }).then((d) => d.token);
export const revokeApiToken = (projectId, tokenId) =>
  api(`/api/projects/${projectId}/tokens/${tokenId}`, { method: "DELETE" });

// --- project webhooks & Slack credentials ---
export const listProjectWebhooks = (projectId) =>
  api(`/api/projects/${projectId}/webhooks`).then((d) => d.webhooks);
export const createProjectWebhook = (projectId, { url, event_type, kind }) =>
  api(`/api/projects/${projectId}/webhooks`, {
    method: "POST",
    body: JSON.stringify({ url, event_type, kind }),
  }).then((d) => d.webhook);
export const setWebhookActive = (webhookId, active) =>
  api(`/api/webhooks/${webhookId}/active`, {
    method: "POST",
    body: JSON.stringify({ active }),
  }).then((d) => d.webhook);
export const deleteWebhook = (webhookId) => api(`/api/webhooks/${webhookId}`, { method: "DELETE" });
export const getSlackCredentialStatus = (projectId) =>
  api(`/api/projects/${projectId}/slack-credentials`);
export const setSlackCredential = (projectId, botToken) =>
  api(`/api/projects/${projectId}/slack-credentials`, {
    method: "POST",
    body: JSON.stringify({ bot_token: botToken }),
  });

// --- admin users ---
export const listAdminUsers = () => api("/api/admin/users").then((d) => d.users);
export const listUserMemberships = (userId) =>
  api(`/api/admin/users/${userId}/memberships`).then((d) => d.memberships);
export const deleteAdminUser = (userId) => api(`/api/admin/users/${userId}`, { method: "DELETE" });
export const assignUserToProject = (userId, projectId, role) =>
  api(`/api/admin/users/${userId}/projects`, {
    method: "POST",
    body: JSON.stringify({ project_id: projectId, role }),
  }).then((d) => d.member);
export const grantPlatformPermission = (userId, perm) =>
  api(`/api/admin/users/${userId}/platform-permissions`, {
    method: "POST",
    body: JSON.stringify({ permission: perm }),
  });
export const revokePlatformPermission = (userId, perm) =>
  api(`/api/admin/users/${userId}/platform-permissions/${encodeURIComponent(perm)}`, {
    method: "DELETE",
  });

// --- admin invitations ---
export const listInvitations = () => api("/api/admin/invitations").then((d) => d.invitations);
export const createInvitation = (email, ttlDays) =>
  api("/api/admin/invitations", {
    method: "POST",
    body: JSON.stringify({ email, ttl_days: ttlDays }),
  }).then((d) => d.invitation);
export const revokeInvitation = (token) =>
  api(`/api/admin/invitations/${encodeURIComponent(token)}`, { method: "DELETE" });

// --- intake sessions ---
export const listIntakeSessions = () => api("/api/intake/sessions");
export const getIntakeSession = (sessionId) => api(`/api/intake/sessions/${sessionId}`);
export const createIntakeSession = () => api("/api/intake/sessions", { method: "POST" });

export const confirmIntake = (sessionId) =>
  api(`/api/intake/sessions/${sessionId}/confirm`, { method: "POST" });

export const abandonIntake = (sessionId) =>
  api(`/api/intake/sessions/${sessionId}/abandon`, { method: "POST" });

export const streamIntakeMessage = (sessionId, message, cbs) =>
  streamAI(`/api/intake/sessions/${sessionId}/stream`, { message }, cbs);

export const setDraftSpec = (sessionId, draftSpecPatch, baseUpdatedAt) =>
  api(`/api/intake/sessions/${sessionId}/draft_spec`, {
    method: "PATCH",
    body: JSON.stringify({ draft_spec: draftSpecPatch, base_updated_at: baseUpdatedAt }),
  });

// --- backlog ---
export const listBacklogItems = (projectId, status, type) => {
  const params = new URLSearchParams();
  if (status) params.set("status", status);
  if (type) params.set("type", type);
  const qs = params.toString();
  return api(`/api/projects/${projectId}/backlog${qs ? `?${qs}` : ""}`).then((d) => d.items);
};

export const createBacklogItem = (projectId, title, body, type) =>
  api(`/api/projects/${projectId}/backlog`, {
    method: "POST",
    body: JSON.stringify({ title, body, type }),
  });

export const voteBacklogItem = (itemId) => api(`/api/backlog/${itemId}/vote`, { method: "POST" });

export const patchBacklogItem = (itemId, patch) =>
  api(`/api/backlog/${itemId}`, { method: "PATCH", body: JSON.stringify(patch) });

export const refineBacklogItems = (projectId, itemIds) =>
  api(`/api/projects/${projectId}/backlog/refine`, {
    method: "POST",
    body: JSON.stringify({ item_ids: itemIds }),
  });

export const getJobBacklogSources = (jobId) =>
  api(`/api/jobs/${jobId}/backlog-sources`).then((d) => d.items);

// --- decisions ---
export const listDecisions = (projectId, epicId) => {
  const params = new URLSearchParams();
  if (epicId != null) params.set("epic_id", String(epicId));
  const qs = params.toString();
  return api(`/api/projects/${projectId}/decisions${qs ? `?${qs}` : ""}`).then((d) => d.decisions);
};

export const getDecision = (projectId, filename) =>
  api(`/api/projects/${projectId}/decisions/${encodeURIComponent(filename)}`);
