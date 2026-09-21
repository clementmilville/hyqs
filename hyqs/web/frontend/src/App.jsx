import { useEffect, useMemo, useState } from "react";
import { getToken, clearToken, getMe, fetchRolePermissions, listProjects, logout } from "./api.js";
import { RoleContext } from "./context.js";
import {
  WORKSPACE_TABS,
  ADMIN_TABS,
  ACCESS_TABS,
  COMMAND_CENTER_TABS,
  EPIC_TABS,
  PLAN_TABS,
  HISTORY_TABS,
  DEFAULT_PERMISSIONS,
  ROLE_NAMES,
  DEFAULT_WORK_VIEW,
  DEFAULT_WORK_GROUPING,
  validateWorkState,
} from "./constants.js";
import { OAuthGate } from "./components/OAuthGate.jsx";
import { Spinner } from "./components/Spinner.jsx";
import { Sidebar, SidebarHamburger, useSidebarCollapsed } from "./components/Sidebar.jsx";
import { WorkspaceShell } from "./views/WorkspaceShell.jsx";
import { AdminConsole } from "./views/AdminConsole.jsx";
import { ToastProvider } from "./components/Toast.jsx";
import { CommandPalette } from "./components/CommandPalette.jsx";
import { BottomTabBar } from "./components/BottomTabBar.jsx";

export { useRole } from "./context.js";

// Default sub-tab for each wsTab that has one — used by parseHash, hashForWorkspace,
// and navWorkspace so the three stay in lockstep.
const SUBTAB_DEFAULT = { plan: "epics", history: "changelog" };

export function parseHash(hash = window.location.hash) {
  const raw = hash.replace(/^#/, "");
  const [path, query = ""] = raw.split("?", 2);
  const parts = path.split("/").filter(Boolean);
  const params = new URLSearchParams(query);
  const defaultWorkState = validateWorkState();
  if (parts[0] === "admin") {
    // #admin/access/{tab} and the legacy #admin/roles|invitations|users|audit
    // (with an optional trailing user id for #admin/access/users/{id} or the
    // legacy #admin/users/{id}) both resolve into the Access group.
    if (parts[1] === "access" || ACCESS_TABS.includes(parts[1])) {
      const subTab =
        parts[1] === "access" ? (ACCESS_TABS.includes(parts[2]) ? parts[2] : "roles") : parts[1];
      const idPart = parts[1] === "access" ? parts[3] : parts[2];
      const adminUserId =
        subTab === "users" && idPart && !Number.isNaN(Number(idPart)) ? Number(idPart) : null;
      return {
        context: "admin",
        projectId: null,
        wsTab: null,
        adminTab: "access",
        adminUserId,
        epicId: null,
        epicTab: null,
        subTab,
        jobId: null,
      };
    }
    // Legacy #admin/overview and #admin/fleet fold into Command Center.
    const adminTab = ADMIN_TABS.includes(parts[1]) ? parts[1] : "command-center";
    const subTab =
      adminTab === "command-center"
        ? COMMAND_CENTER_TABS.includes(parts[2])
          ? parts[2]
          : "pipeline"
        : null;
    return {
      context: "admin",
      projectId: null,
      wsTab: null,
      adminTab,
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab,
      jobId: null,
    };
  }
  if (parts[0] === "workspace") {
    const projectId = parts[1] ? Number(parts[1]) || null : null;

    // Legacy: #workspace/{pid}/epic/{id}/{tab} -> Plan > Epics > epic detail
    if (parts[2] === "epic") {
      const epicId = parts[3] && !Number.isNaN(Number(parts[3])) ? Number(parts[3]) : null;
      const epicTab = EPIC_TABS.includes(parts[4]) ? parts[4] : "jobs";
      return {
        context: "workspace",
        projectId,
        wsTab: "plan",
        adminTab: null,
        adminUserId: null,
        epicId,
        epicTab,
        subTab: "epics",
        jobId: null,
      };
    }

    // Legacy: #workspace/{pid}/backlog -> Plan > Backlog
    if (parts[2] === "backlog") {
      return {
        context: "workspace",
        projectId,
        wsTab: "plan",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: "backlog",
        jobId: null,
      };
    }

    // Legacy: #workspace/{pid}/runtime -> Work (Runtime's surfaces now live in
    // the Work lanes, see CONVENTIONS.md section 9).
    if (parts[2] === "runtime") {
      return {
        context: "workspace",
        projectId,
        wsTab: "work",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: null,
        jobId: null,
        workState: defaultWorkState,
      };
    }

    // Legacy: #workspace/{pid}/changelog|decisions -> History > Changelog|Decisions
    if (parts[2] === "changelog" || parts[2] === "decisions") {
      return {
        context: "workspace",
        projectId,
        wsTab: "history",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: parts[2],
        jobId: null,
      };
    }

    // Legacy: #workspace/{pid}/insights -> Analytics
    if (parts[2] === "insights") {
      return {
        context: "workspace",
        projectId,
        wsTab: "analytics",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: null,
        jobId: null,
      };
    }

    // Legacy: #workspace/{pid}/performance -> Analytics
    if (parts[2] === "performance") {
      return {
        context: "workspace",
        projectId,
        wsTab: "analytics",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: null,
        jobId: null,
      };
    }

    if (parts[2] === "plan") {
      if (parts[3] === "epic") {
        const epicId = parts[4] && !Number.isNaN(Number(parts[4])) ? Number(parts[4]) : null;
        const epicTab = EPIC_TABS.includes(parts[5]) ? parts[5] : "jobs";
        return {
          context: "workspace",
          projectId,
          wsTab: "plan",
          adminTab: null,
          adminUserId: null,
          epicId,
          epicTab,
          subTab: "epics",
          jobId: null,
        };
      }
      const subTab = PLAN_TABS.includes(parts[3]) ? parts[3] : "epics";
      return {
        context: "workspace",
        projectId,
        wsTab: "plan",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab,
        jobId: null,
      };
    }

    if (parts[2] === "history") {
      const subTab = HISTORY_TABS.includes(parts[3]) ? parts[3] : "changelog";
      return {
        context: "workspace",
        projectId,
        wsTab: "history",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab,
        jobId: null,
      };
    }

    if (parts[2] === "analytics") {
      return {
        context: "workspace",
        projectId,
        wsTab: "analytics",
        adminTab: null,
        adminUserId: null,
        epicId: null,
        epicTab: null,
        subTab: null,
        jobId: null,
      };
    }

    const wsTab = WORKSPACE_TABS.includes(parts[2]) ? parts[2] : "dashboard";
    const workState =
      wsTab === "work"
        ? validateWorkState({
            view: parts[3],
            epicId: params.get("epic"),
            search: params.get("search") ?? "",
            grouping: params.get("group"),
            selectedJobId: params.get("selected"),
          })
        : defaultWorkState;
    return {
      context: "workspace",
      projectId,
      wsTab,
      adminTab: null,
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab: SUBTAB_DEFAULT[wsTab] ?? null,
      jobId: null,
      workState,
    };
  }
  if (parts[0] === "project" && parts[1] && parts[2] === "job" && parts[3]) {
    return {
      context: "workspace",
      projectId: Number(parts[1]) || null,
      wsTab: "work",
      adminTab: null,
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab: null,
      jobId: Number(parts[3]) || null,
      workState: defaultWorkState,
    };
  }
  if (parts[0] === "fleet" || parts[0] === "supervisor") {
    return {
      context: "admin",
      projectId: null,
      wsTab: null,
      adminTab: "command-center",
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab: "pipeline",
      jobId: null,
      workState: defaultWorkState,
    };
  }
  if (parts[0] === "usage") {
    return {
      context: "admin",
      projectId: null,
      wsTab: null,
      adminTab: "command-center",
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab: "pipeline",
      jobId: null,
      workState: defaultWorkState,
    };
  }
  if (parts[0] === "projects" && parts[1]) {
    return {
      context: "workspace",
      projectId: Number(parts[1]) || null,
      wsTab: "work",
      adminTab: null,
      adminUserId: null,
      epicId: null,
      epicTab: null,
      subTab: null,
      jobId: null,
      workState: defaultWorkState,
    };
  }
  const lastId = Number(localStorage.getItem("hyqs_last_project")) || null;
  return {
    context: "workspace",
    projectId: lastId,
    wsTab: "dashboard",
    adminTab: null,
    adminUserId: null,
    epicId: null,
    epicTab: null,
    subTab: null,
    jobId: null,
    workState: defaultWorkState,
  };
}

export function hashForWorkspace(projectId, wsTab, subTab, workState) {
  if (wsTab === "work") {
    const state = validateWorkState(workState);
    const params = new URLSearchParams();
    if (state.epicId != null) params.set("epic", String(state.epicId));
    if (state.search) params.set("search", state.search);
    if (state.grouping !== DEFAULT_WORK_GROUPING) params.set("group", state.grouping);
    if (state.selectedJobId != null) params.set("selected", String(state.selectedJobId));
    const query = params.toString();
    return `#workspace/${projectId ?? ""}/work/${state.view ?? DEFAULT_WORK_VIEW}${
      query ? `?${query}` : ""
    }`;
  }
  if (SUBTAB_DEFAULT[wsTab]) {
    return `#workspace/${projectId ?? ""}/${wsTab}/${subTab ?? SUBTAB_DEFAULT[wsTab]}`;
  }
  return `#workspace/${projectId ?? ""}/${wsTab ?? "dashboard"}`;
}

function hashForEpic(projectId, epicId, epicTab) {
  return `#workspace/${projectId ?? ""}/plan/epic/${epicId ?? ""}/${epicTab ?? "jobs"}`;
}

export function hashForJob(projectId, jobId) {
  return `#project/${projectId ?? ""}/job/${jobId ?? ""}`;
}

function hashForAdmin(adminTab, subTab) {
  if (adminTab === "access") {
    return `#admin/access/${subTab ?? "roles"}`;
  }
  if (adminTab === "command-center") {
    return `#admin/command-center/${subTab ?? "pipeline"}`;
  }
  return `#admin/${adminTab}`;
}

function hashForAdminUser(userId) {
  return `#admin/access/users/${userId}`;
}

export default function App() {
  const [authed, setAuthed] = useState(!!getToken());
  const [checkingCookieAuth, setCheckingCookieAuth] = useState(!getToken());
  const [memberships, setMemberships] = useState({});
  const [isPlatformAdmin, setIsPlatformAdmin] = useState(false);
  const [permissions, setPermissions] = useState(DEFAULT_PERMISSIONS);
  const [platformPermissions, setPlatformPermissions] = useState(new Set());
  const [authLoading, setAuthLoading] = useState(true);
  const [authError, setAuthError] = useState(false);
  const parsed = parseHash();
  const [context, setContext] = useState(parsed.context);
  const [projectId, setProjectId] = useState(parsed.projectId);
  const [wsTab, setWsTab] = useState(parsed.wsTab || "dashboard");
  const [adminTab, setAdminTab] = useState(parsed.adminTab || "command-center");
  const [adminUserId, setAdminUserId] = useState(parsed.adminUserId ?? null);
  const [epicId, setEpicId] = useState(parsed.epicId ?? null);
  const [epicTab, setEpicTab] = useState(parsed.epicTab ?? null);
  const [subTab, setSubTab] = useState(parsed.subTab ?? "epics");
  const [jobId, setJobId] = useState(parsed.jobId ?? null);
  const [workState, setWorkState] = useState(validateWorkState(parsed.workState));
  const [projects, setProjects] = useState([]);
  const [projectsLoaded, setProjectsLoaded] = useState(false);
  const [collapsed, toggleCollapsed] = useSidebarCollapsed();
  const [mobileOpen, setMobileOpen] = useState(false);
  const [cmdOpen, setCmdOpen] = useState(false);

  const effectiveRole = useMemo(() => {
    if (isPlatformAdmin) return "platform_admin";
    if (projectId == null) return "viewer";
    return memberships[String(projectId)] ?? "viewer";
  }, [isPlatformAdmin, memberships, projectId]);

  const can = (action) =>
    platformPermissions.has(action) || (permissions[effectiveRole]?.has(action) ?? false);

  const reloadProjects = () =>
    listProjects()
      .then((p) => {
        setProjects(p);
        setProjectsLoaded(true);
      })
      .catch(() => {});

  const retryAuth = () => {
    setAuthError(false);
    setAuthLoading(true);
  };

  useEffect(() => {
    if (!authed) return;
    if (!authLoading) return;

    let cancelled = false;

    async function fetchWithRetry(fn) {
      for (let i = 0; i < 4; i++) {
        try {
          return await fn();
        } catch (_) {
          if (cancelled) return undefined;
          if (i < 3) await new Promise((r) => setTimeout(r, Math.pow(2, i) * 1000));
        }
      }
      return undefined;
    }

    async function loadAuth() {
      const [roleRes, permRes] = await Promise.all([
        fetchWithRetry(getMe),
        fetchWithRetry(fetchRolePermissions),
      ]);
      if (cancelled) return;
      if (roleRes === undefined) {
        setAuthError(true);
        setAuthLoading(false);
        return;
      }
      if (roleRes) {
        setIsPlatformAdmin(roleRes.is_platform_admin ?? false);
        setMemberships(roleRes.memberships ?? {});
        setPlatformPermissions(new Set(roleRes.platform_permissions ?? []));
      }
      if (permRes) {
        const sets = {};
        for (const r of ROLE_NAMES) {
          const dbPerms = permRes[r] ?? [];
          sets[r] = new Set([...(DEFAULT_PERMISSIONS[r] ?? []), ...dbPerms]);
        }
        setPermissions(sets);
      }
      setAuthError(false);
      setAuthLoading(false);
    }

    loadAuth();
    return () => {
      cancelled = true;
    };
  }, [authed, authLoading]);

  useEffect(() => {
    if (authed) setAuthLoading(true);
  }, [authed]);

  useEffect(() => {
    if (!authed) return;
    reloadProjects();
    const id = setInterval(reloadProjects, 5000);
    return () => clearInterval(id);
  }, [authed]);

  useEffect(() => {
    const onUnauth = () => setAuthed(false);
    window.addEventListener("hyqs-unauthorized", onUnauth);
    return () => window.removeEventListener("hyqs-unauthorized", onUnauth);
  }, []);

  // A cookie-authenticated session (set by the OAuth callback) has no
  // localStorage token, so probe /api/me once on mount before falling back
  // to the login gate.
  useEffect(() => {
    if (getToken()) return;
    let cancelled = false;
    getMe()
      .then(() => {
        if (!cancelled) setAuthed(true);
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setCheckingCookieAuth(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    const onPop = () => {
      const {
        context: c,
        projectId: p,
        wsTab: w,
        adminTab: a,
        adminUserId: au,
        epicId: eid,
        epicTab: et,
        subTab: st,
        jobId: jid,
        workState: nextWorkState,
      } = parseHash();
      setContext(c);
      if (p != null) setProjectId(p);
      if (w) setWsTab(w);
      if (a) setAdminTab(a);
      setAdminUserId(au);
      setEpicId(eid ?? null);
      setEpicTab(et ?? null);
      setSubTab(st ?? "epics");
      setJobId(jid ?? null);
      setWorkState(validateWorkState(nextWorkState));
    };
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    const onKeyDown = (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setCmdOpen(true);
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  function navWorkspace(pid, tab, subTabArg) {
    const p = pid !== undefined ? pid : projectId;
    const t = tab ?? wsTab;
    const st = SUBTAB_DEFAULT[t] ? (subTabArg ?? SUBTAB_DEFAULT[t]) : subTab;
    setContext("workspace");
    setProjectId(p);
    if (tab) setWsTab(t);
    if (SUBTAB_DEFAULT[t]) setSubTab(st);
    setEpicId(null);
    setEpicTab(null);
    setJobId(null);
    setMobileOpen(false);
    window.history.pushState(null, "", hashForWorkspace(p, t, st, workState));
  }

  function navWorkState(nextState) {
    const next = validateWorkState({ ...workState, ...nextState });
    setContext("workspace");
    setWsTab("work");
    setWorkState(next);
    setJobId(null);
    window.history.pushState(null, "", hashForWorkspace(projectId, "work", null, next));
  }

  function navEpic(pid, eid, tab) {
    const p = pid !== undefined ? pid : projectId;
    const t = tab ?? epicTab ?? "jobs";
    setContext("workspace");
    setProjectId(p);
    setWsTab("plan");
    setSubTab("epics");
    setEpicId(eid);
    setEpicTab(t);
    setMobileOpen(false);
    window.history.pushState(null, "", hashForEpic(p, eid, t));
  }

  function navJob(pid, jid) {
    const p = pid !== undefined ? pid : projectId;
    const origin =
      wsTab === "work" && jobId == null
        ? { hash: hashForWorkspace(p, "work", null, workState), scrollY: window.scrollY }
        : null;
    setContext("workspace");
    setProjectId(p);
    setWsTab("work");
    setJobId(jid);
    setMobileOpen(false);
    window.history.pushState(origin ? { workOrigin: origin } : null, "", hashForJob(p, jid));
  }

  function backFromJob() {
    const origin = window.history.state?.workOrigin;
    if (origin?.hash) {
      const restored = parseHash(origin.hash).workState;
      setJobId(null);
      setWsTab("work");
      setWorkState(restored);
      window.history.pushState(null, "", origin.hash);
      window.requestAnimationFrame(() => window.scrollTo(0, origin.scrollY ?? 0));
      return;
    }
    navWorkspace(projectId, "work");
  }

  function navAdmin(tab, subTabArg) {
    const t = tab ?? adminTab;
    const st =
      t === "access"
        ? (subTabArg ?? (adminTab === "access" ? subTab : "roles"))
        : t === "command-center"
          ? (subTabArg ??
            (adminTab === "command-center" && COMMAND_CENTER_TABS.includes(subTab)
              ? subTab
              : "pipeline"))
          : null;
    setContext("admin");
    if (tab) setAdminTab(t);
    if (st) setSubTab(st);
    setAdminUserId(null);
    setMobileOpen(false);
    window.history.pushState(null, "", hashForAdmin(t, st));
  }

  function navAdminUser(userId) {
    setContext("admin");
    setAdminTab("access");
    setSubTab("users");
    setAdminUserId(userId);
    setMobileOpen(false);
    window.history.pushState(
      null,
      "",
      userId != null ? hashForAdminUser(userId) : hashForAdmin("access", "users")
    );
  }

  if (checkingCookieAuth) return <Spinner />;
  if (!authed) return <OAuthGate onSave={() => setAuthed(true)} />;

  const signOut = async () => {
    await logout();
    clearToken();
    setAuthed(false);
  };

  const currentProject = projects.find((p) => p.id === projectId) ?? projects[0] ?? null;
  const currentProjectId = currentProject?.id ?? null;

  return (
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: effectiveRole, can, setPermissions, authLoading, authError, retryAuth }}
      >
        <div className="app-shell">
          <Sidebar
            context={context}
            wsTab={wsTab}
            adminTab={adminTab}
            projects={projects}
            currentProjectId={currentProjectId}
            onNavWorkspace={(tab) => navWorkspace(currentProjectId, tab)}
            onNavAdmin={navAdmin}
            onSwitch={(id) => navWorkspace(id, wsTab)}
            onSignOut={signOut}
            collapsed={collapsed}
            onToggle={toggleCollapsed}
            mobileOpen={mobileOpen}
            onMobileClose={() => setMobileOpen(false)}
          />
          <div className="app-content">
            <SidebarHamburger onClick={() => setMobileOpen(true)} />
            <div className="content-wrap">
              {context === "workspace" ? (
                <WorkspaceShell
                  projectId={projectId}
                  wsTab={wsTab}
                  epicId={epicId}
                  epicTab={epicTab}
                  subTab={subTab}
                  jobId={jobId}
                  workState={workState}
                  projects={projects}
                  projectsLoaded={projectsLoaded}
                  onNavWorkspace={navWorkspace}
                  onNavEpic={navEpic}
                  onNavJob={navJob}
                  onWorkStateChange={navWorkState}
                  onBackJob={backFromJob}
                  onNavAdmin={navAdmin}
                  onReloadProjects={reloadProjects}
                />
              ) : (
                <AdminConsole
                  adminTab={adminTab}
                  subTab={subTab}
                  adminUserId={adminUserId}
                  onNavAdmin={navAdmin}
                  onNavAdminUser={navAdminUser}
                  onNavWorkspace={navWorkspace}
                  projects={projects}
                  projectsLoaded={projectsLoaded}
                  onReloadProjects={reloadProjects}
                />
              )}
            </div>
          </div>
          <BottomTabBar
            context={context}
            active={context === "admin" ? adminTab : wsTab}
            can={can}
            projects={projects}
            currentProjectId={currentProjectId}
            onNavAdmin={navAdmin}
            onNavigate={(tab) => navWorkspace(currentProjectId, tab)}
            onSwitch={(id) => navWorkspace(id, wsTab)}
            onSignOut={signOut}
          />
        </div>
        <CommandPalette
          open={cmdOpen}
          onClose={() => setCmdOpen(false)}
          projects={projects}
          currentProjectId={currentProjectId}
          onNavWorkspace={navWorkspace}
          onNavAdmin={navAdmin}
        />
      </RoleContext.Provider>
    </ToastProvider>
  );
}
