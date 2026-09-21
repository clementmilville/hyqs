import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, waitFor, screen } from "@testing-library/react";
import App, { parseHash, hashForJob, hashForWorkspace } from "./App.jsx";
import { getToken, getMe } from "./api.js";

// Stub all api.js exports so no network calls escape to the test environment.
// getToken must return a truthy value so App skips <OAuthGate> and renders
// the full shell on the initial paint.
vi.mock("./api.js", () => {
  const res = (v = null) => vi.fn(() => Promise.resolve(v));
  const es = () => vi.fn(() => ({ close: vi.fn(), addEventListener: vi.fn() }));
  return {
    getToken: vi.fn(() => "test-token"),
    setToken: vi.fn(),
    clearToken: vi.fn(),
    verifyToken: res(true),
    getMe: res({ role: "viewer" }),
    fetchRolePermissions: res({}),
    listProjects: res([]),
    listJobsFiltered: res([]),
    createJob: res({}),
    cancelJob: res(),
    retryJob: res(),
    setJobPriority: res(),
    archiveJob: res(),
    unarchiveJob: res(),
    getUsage: res({}),
    getJobEvents: res([]),
    getJobDiff: res({ diff: "", base: "", branch: "", truncated: false }),
    provisionProject: res({}),
    updateProject: res({}),
    getAgentMeta: res({ providers: [], tasks: [] }),
    listAgents: res([]),
    agentStats: res([]),
    createAgent: res({}),
    updateAgent: res({}),
    deleteAgent: res(),
    listEpics: res([]),
    archiveEpic: res(),
    unarchiveEpic: res(),
    createEpic: res({}),
    streamAI: vi.fn(() => ({ cancel: vi.fn() })),
    streamChat: vi.fn(() => ({ cancel: vi.fn() })),
    streamSuggest: vi.fn(() => ({ cancel: vi.fn() })),
    streamIntakeMessage: vi.fn(() => ({ cancel: vi.fn() })),
    streamJobs: es(),
    streamLogs: es(),
    getWorkers: res({ workers: [] }),
    streamWorkers: es(),
    getSupervisor: res({}),
    streamSupervisor: es(),
    getProjectsRuntime: res({ projects: [] }),
    getProjectRuntime: res({}),
    listProviders: res([]),
    clearProviderPause: res(),
    patchRolePermission: res(),
    getPerfHeadline: res({}),
    getPerfStageStats: res([]),
    getPerfSlowestJobs: res([]),
    listProjectMembers: res([]),
    addProjectMember: res({}),
    removeProjectMember: res(),
    updateMemberRole: res({}),
    createIntakeSession: res({}),
    confirmIntake: res(),
    abandonIntake: res(),
    listInvitations: res([]),
    createInvitation: res({}),
    revokeInvitation: res(),
  };
});

describe("App shell layout", () => {
  it("renders .app-shell > .app-content > .content-wrap", () => {
    const { container } = render(<App />);

    const shell = container.querySelector(".app-shell");
    expect(shell).toBeInTheDocument();

    const content = shell?.querySelector(".app-content");
    expect(content).not.toBeNull();

    const wrap = content?.querySelector(".content-wrap");
    expect(wrap).not.toBeNull();
  });

  it(".content-wrap is not a direct child of .app-shell", () => {
    const { container } = render(<App />);
    const shell = container.querySelector(".app-shell");
    const directWrap = Array.from(shell?.children ?? []).find((el) =>
      el.classList.contains("content-wrap")
    );
    expect(directWrap).toBeUndefined();
  });
});

describe("cookie-based auth bootstrap", () => {
  afterEach(() => {
    getToken.mockReturnValue("test-token");
  });

  it("renders the authenticated shell when a cookie session probe succeeds with no stored token", async () => {
    getToken.mockReturnValue("");
    getMe.mockResolvedValueOnce({ role: "viewer" });

    const { container } = render(<App />);

    await waitFor(() => {
      expect(container.querySelector(".app-shell")).toBeInTheDocument();
    });
    expect(screen.queryByRole("heading", { name: "Hyqs" })).not.toBeInTheDocument();
  });
});

describe("parseHash epic route", () => {
  const originalHash = window.location.hash;

  beforeEach(() => {
    localStorage.clear();
  });

  afterEach(() => {
    window.location.hash = originalHash;
  });

  it("parses #workspace/{projectId}/plan/epic/{epicId}/{tab}", () => {
    window.location.hash = "#workspace/5/plan/epic/9/architect";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "plan",
      subTab: "epics",
      epicId: 9,
      epicTab: "architect",
    });
  });

  it("defaults the epic tab to 'jobs' when no tab segment is present", () => {
    window.location.hash = "#workspace/5/plan/epic/9";
    const parsed = parseHash();
    expect(parsed).toMatchObject({ projectId: 5, epicId: 9, epicTab: "jobs" });
  });

  it("falls back to 'jobs' for an unrecognized epic tab segment", () => {
    window.location.hash = "#workspace/5/plan/epic/9/bogus";
    const parsed = parseHash();
    expect(parsed.epicTab).toBe("jobs");
  });

  it("resolves epicId to null for a plain workspace hash", () => {
    window.location.hash = "#workspace/5/work";
    const parsed = parseHash();
    expect(parsed).toMatchObject({ projectId: 5, wsTab: "work", epicId: null, epicTab: null });
  });

  it("legacy #workspace/{pid}/epic/{id}/{tab} redirects into Plan > Epics", () => {
    window.location.hash = "#workspace/5/epic/9/architect";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "plan",
      subTab: "epics",
      epicId: 9,
      epicTab: "architect",
    });
  });

  it("legacy #workspace/{pid}/backlog redirects into Plan > Backlog", () => {
    window.location.hash = "#workspace/5/backlog";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "plan",
      subTab: "backlog",
      epicId: null,
      epicTab: null,
    });
  });

  it("parses #workspace/{pid}/plan/backlog directly", () => {
    window.location.hash = "#workspace/5/plan/backlog";
    const parsed = parseHash();
    expect(parsed).toMatchObject({ projectId: 5, wsTab: "plan", subTab: "backlog" });
  });

  it("falls back to 'epics' for an unrecognized plan sub-tab", () => {
    window.location.hash = "#workspace/5/plan/bogus";
    const parsed = parseHash();
    expect(parsed.subTab).toBe("epics");
  });

  it("legacy #workspace/{pid}/runtime redirects into Work", () => {
    window.location.hash = "#workspace/3/runtime";
    const parsed = parseHash();
    expect(parsed).toMatchObject({ context: "workspace", projectId: 3, wsTab: "work" });
  });

  it("parses #workspace/{pid}/history/{tab} directly", () => {
    window.location.hash = "#workspace/5/history/decisions";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "history",
      subTab: "decisions",
    });
  });

  it("defaults history sub-tab to 'changelog' for an unrecognized segment", () => {
    window.location.hash = "#workspace/5/history/bogus";
    const parsed = parseHash();
    expect(parsed.subTab).toBe("changelog");
  });

  it("parses #workspace/{pid}/analytics/{tab} directly", () => {
    window.location.hash = "#workspace/5/analytics/pipeline";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "analytics",
      subTab: null,
    });
  });

  it("ignores any trailing segment for analytics (no sub-tabs)", () => {
    window.location.hash = "#workspace/5/analytics/bogus";
    const parsed = parseHash();
    expect(parsed.subTab).toBe(null);
  });

  it("legacy #workspace/{pid}/changelog redirects into History > Changelog", () => {
    window.location.hash = "#workspace/5/changelog";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "history",
      subTab: "changelog",
    });
  });

  it("legacy #workspace/{pid}/decisions redirects into History > Decisions", () => {
    window.location.hash = "#workspace/5/decisions";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "history",
      subTab: "decisions",
    });
  });

  it("legacy #workspace/{pid}/insights redirects into Analytics", () => {
    window.location.hash = "#workspace/5/insights";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "analytics",
      subTab: null,
    });
  });

  it("legacy #workspace/{pid}/performance redirects into Analytics", () => {
    window.location.hash = "#workspace/5/performance";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "analytics",
      subTab: null,
    });
  });
});

describe("parseHash admin routes", () => {
  const originalHash = window.location.hash;

  afterEach(() => {
    window.location.hash = originalHash;
  });

  it.each(["#admin", "#admin/overview", "#admin/fleet", "#fleet", "#supervisor", "#usage"])(
    "resolves %s to Command Center",
    (hash) => {
      window.location.hash = hash;
      const parsed = parseHash();
      expect(parsed).toMatchObject({ context: "admin", adminTab: "command-center" });
    }
  );

  it.each(["roles", "invitations", "users", "audit"])(
    "parses #admin/access/%s into the Access group",
    (tab) => {
      window.location.hash = `#admin/access/${tab}`;
      const parsed = parseHash();
      expect(parsed).toMatchObject({ context: "admin", adminTab: "access", subTab: tab });
    }
  );

  it.each(["roles", "invitations", "users", "audit"])(
    "legacy #admin/%s redirects into the Access group",
    (tab) => {
      window.location.hash = `#admin/${tab}`;
      const parsed = parseHash();
      expect(parsed).toMatchObject({ context: "admin", adminTab: "access", subTab: tab });
    }
  );

  it("resolves #admin/access/users/42 to adminUserId 42", () => {
    window.location.hash = "#admin/access/users/42";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "admin",
      adminTab: "access",
      subTab: "users",
      adminUserId: 42,
    });
  });

  it("legacy #admin/users/42 also resolves to adminUserId 42", () => {
    window.location.hash = "#admin/users/42";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "admin",
      adminTab: "access",
      subTab: "users",
      adminUserId: 42,
    });
  });
});

describe("parseHash command-center sub-tabs", () => {
  const originalHash = window.location.hash;

  afterEach(() => {
    window.location.hash = originalHash;
  });

  it.each(["pipeline", "deployments", "runtime"])(
    "parses #admin/command-center/%s into the Command Center subTab",
    (tab) => {
      window.location.hash = `#admin/command-center/${tab}`;
      const parsed = parseHash();
      expect(parsed).toMatchObject({ context: "admin", adminTab: "command-center", subTab: tab });
    }
  );

  it("defaults #admin/command-center to the pipeline subTab", () => {
    window.location.hash = "#admin/command-center";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "admin",
      adminTab: "command-center",
      subTab: "pipeline",
    });
  });

  it("ignores an unknown sub-tab and falls back to pipeline", () => {
    window.location.hash = "#admin/command-center/bogus";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "admin",
      adminTab: "command-center",
      subTab: "pipeline",
    });
  });
});

describe("parseHash job route", () => {
  const originalHash = window.location.hash;

  afterEach(() => {
    window.location.hash = originalHash;
  });

  it("parses #project/{projectId}/job/{jobId}", () => {
    window.location.hash = "#project/5/job/42";
    const parsed = parseHash();
    expect(parsed).toMatchObject({
      context: "workspace",
      projectId: 5,
      wsTab: "work",
      jobId: 42,
    });
  });

  it("hashForJob(5, 42) round-trips through parseHash to the same projectId and jobId", () => {
    window.location.hash = hashForJob(5, 42);
    expect(window.location.hash).toBe("#project/5/job/42");
    const parsed = parseHash();
    expect(parsed).toMatchObject({ projectId: 5, jobId: 42 });
  });

  it("resolves jobId to null for a plain workspace hash", () => {
    window.location.hash = "#workspace/5/work";
    const parsed = parseHash();
    expect(parsed.jobId).toBeNull();
  });
});

describe("Work route state", () => {
  it("defaults plain, invalid, runtime, and legacy project routes to Focus", () => {
    for (const hash of [
      "#workspace/5/work",
      "#workspace/5/work/not-a-view?group=nope",
      "#workspace/5/runtime",
      "#projects/5",
    ]) {
      expect(parseHash(hash).workState).toMatchObject({
        view: "focus",
        grouping: "status",
        epicId: null,
        search: "",
        selectedJobId: null,
      });
    }
  });

  it("round-trips supported Work state", () => {
    const hash = hashForWorkspace(5, "work", null, {
      view: "history",
      epicId: 9,
      search: "release candidate",
      grouping: "epic",
      selectedJobId: 42,
    });

    expect(parseHash(hash)).toMatchObject({
      projectId: 5,
      wsTab: "work",
      workState: {
        view: "history",
        epicId: 9,
        search: "release candidate",
        grouping: "epic",
        selectedJobId: 42,
      },
    });
  });
});
