export const STAGE_EMOJI = {
  queued: "⏳",
  plan: "🧭",
  build: "🔨",
  lint: "🧹",
  test: "✅",
  review: "🔎",
  security: "🔒",
  design_review: "🎨",
  merge: "🔀",
  deploy: "🚀",
  done: "🎉",
  fix: "🩹",
};

export const STATUS_CLASS = {
  done: "ok",
  failed: "bad",
  running: "run",
  deploying: "run",
  pending: "wait",
  cancelled: "bad",
  active: "active",
  paused: "wait",
};

const TERMINAL_STATUSES = new Set(["done", "failed", "cancelled"]);

export function isActiveStatus(status) {
  return !TERMINAL_STATUSES.has(status);
}

export const PRIORITY_LEVELS = { Low: -10, Normal: 0, High: 10, Urgent: 20 };

export const STEPS = [
  "plan",
  "build",
  "lint",
  "test",
  "review",
  "security",
  "design_review",
  "merge",
  "deploy",
  "done",
];

/**
 * Maps job.stage (the backend's offset-dispatch token) to the number of
 * completed nodes, which equals the index of the currently-active step.
 *
 * The offset semantics are intentional: build.run() writes stage=LINT (so the
 * job is *at lint*, active index 2), and lint.run() writes stage=BUILD (so
 * lint is done and test is active, index 3). A naive identity map is wrong.
 * fix→1 loops back to the build node (the coder is re-coding).
 *
 * security→6 now correctly points at the new design_review node (security.run()
 * finished, design_review.run() is executing next). design_review→7 aliases to
 * the same active index as merge→7 — the same pattern security/merge used
 * before design_review existed — because design_review.run() finishing means
 * merge.run() is executing next.
 */
export const STAGE_DONE = {
  queued: 0,
  plan: 1,
  lint: 2, // build.run() sets stage=LINT → lint is now the active step
  build: 3, // lint.run() sets stage=BUILD → lint is done, test is active
  test: 4,
  review: 5,
  security: 6, // security.run() finished → design_review is now the active step
  design_review: 7, // design_review.run() finished → merge is now the active step
  merge: 7,
  deploy: 8,
  done: 9,
  fix: 1, // fix loops back to the build (coding) node
};

export const STAGE_LABEL = {
  queued: "Queued",
  plan: "Plan",
  build: "Build",
  lint: "Lint",
  test: "Test",
  review: "Review",
  security: "Security",
  design_review: "Design Review",
  fix: "Fix",
  merge: "Merge",
  deploy: "Deploy",
};

export const KNOWN_PERMISSIONS = [
  "queue_job",
  "cancel_job",
  "retry_job",
  "archive_job",
  "edit_project",
  "edit_agents",
  "edit_job_deps",
  "resolve_job",
  "manage_members",
  "delete_project",
  "create_project",
  "manage_users",
  "manage_invitations",
  "manage_roles",
  "manage_providers",
  "view_fleet",
  "propose_backlog",
  "triage_backlog",
  "manage_api_tokens",
  "deploy.promote",
  "deploy.offer",
  "deploy.promote_prod",
  "deploy.apply",
  "deploy.view",
];

export const ROLE_NAMES = [
  "viewer",
  "automation_client",
  "contributor",
  "project_admin",
  "platform_admin",
  "release_manager",
  "prod_promoter",
  "client_approver",
];

export const DEFAULT_PERMISSIONS = {
  viewer: new Set(["propose_backlog", "deploy.view"]),
  automation_client: new Set([
    "queue_job",
    "cancel_job",
    "retry_job",
    "edit_job_deps",
    "resolve_job",
  ]),
  release_manager: new Set(["deploy.promote", "deploy.offer", "deploy.view"]),
  prod_promoter: new Set(["deploy.promote", "deploy.offer", "deploy.view", "deploy.promote_prod"]),
  client_approver: new Set(["deploy.apply", "deploy.view"]),
  contributor: new Set([
    "queue_job",
    "cancel_job",
    "retry_job",
    "archive_job",
    "edit_job_deps",
    "resolve_job",
    "propose_backlog",
  ]),
  project_admin: new Set([
    "queue_job",
    "cancel_job",
    "retry_job",
    "archive_job",
    "edit_project",
    "edit_agents",
    "edit_job_deps",
    "resolve_job",
    "manage_members",
    "propose_backlog",
    "triage_backlog",
    "manage_api_tokens",
    "deploy.promote",
    "deploy.offer",
  ]),
  platform_admin: new Set([
    "queue_job",
    "cancel_job",
    "retry_job",
    "archive_job",
    "edit_project",
    "edit_agents",
    "edit_job_deps",
    "resolve_job",
    "manage_members",
    "delete_project",
    "create_project",
    "manage_users",
    "manage_invitations",
    "manage_roles",
    "manage_providers",
    "view_fleet",
    "propose_backlog",
    "triage_backlog",
    "manage_api_tokens",
    "deploy.promote",
    "deploy.offer",
    "deploy.promote_prod",
    "deploy.apply",
    "deploy.view",
  ]),
};

// Whitelisted requeue targets for the guided Resolution panel (must match
// incident_analyst.REQUEUEABLE_STAGES on the backend).
export const REQUEUE_STAGES = ["queued", "lint", "build", "test", "review", "security", "deploy"];

export const EPIC_TABS = ["jobs", "ideate", "architect", "decisions"];

export const PLAN_TABS = ["epics", "backlog"];
export const PLAN_TAB_LABEL = { epics: "Epics", backlog: "Backlog" };

export const HISTORY_TABS = ["changelog", "decisions", "archived"];
export const HISTORY_TAB_LABEL = {
  changelog: "Changelog",
  decisions: "Decisions",
  archived: "Archived",
};

export const WORKSPACE_TABS = ["dashboard", "work", "plan", "history", "analytics", "settings"];
export const WORK_VIEWS = ["focus", "queue", "history"];
export const WORK_VIEW_LABEL = { focus: "Focus", queue: "Queue", history: "History" };
export const DEFAULT_WORK_VIEW = "focus";
export const WORK_GROUPINGS = ["status", "epic"];
export const DEFAULT_WORK_GROUPING = "status";

export function validateWorkState(state = {}) {
  const epicId = Number(state.epicId);
  const selectedJobId = Number(state.selectedJobId);
  return {
    view: WORK_VIEWS.includes(state.view) ? state.view : DEFAULT_WORK_VIEW,
    epicId: Number.isInteger(epicId) && epicId > 0 ? epicId : null,
    search: typeof state.search === "string" ? state.search : "",
    grouping: WORK_GROUPINGS.includes(state.grouping) ? state.grouping : DEFAULT_WORK_GROUPING,
    selectedJobId: Number.isInteger(selectedJobId) && selectedJobId > 0 ? selectedJobId : null,
  };
}
export const ADMIN_TABS = [
  "command-center",
  "projects",
  "new-project",
  "providers",
  "access",
  "usage-cost",
  "visitor-analytics",
];

export const COMMAND_CENTER_TABS = ["pipeline", "deployments", "runtime"];
export const COMMAND_CENTER_TAB_LABEL = {
  pipeline: "Pipeline",
  deployments: "Deployments",
  runtime: "Runtime",
};

export const ADMIN_NAV_PERMISSIONS = {
  "command-center": null,
  projects: "create_project",
  "new-project": "create_project",
  providers: "manage_providers",
  "usage-cost": "view_fleet",
  "visitor-analytics": "view_audit",
};

export const ACCESS_TABS = ["roles", "invitations", "users", "audit"];
export const ACCESS_TAB_LABEL = {
  roles: "Roles",
  invitations: "Invitations",
  users: "Users",
  audit: "Audit",
};
export const ACCESS_TAB_PERMISSIONS = {
  roles: "manage_roles",
  invitations: "manage_invitations",
  users: "manage_users",
  audit: "view_audit",
};

// True if any of the four Access sub-tabs (Roles/Invitations/Users/Audit) is
// reachable — governs whether the "Access" nav item itself is shown.
export function canSeeAccessGroup(can) {
  return Object.values(ACCESS_TAB_PERMISSIONS).some((p) => can(p));
}

// True if the platform-admin nav section should render at all.
export function canSeeAdminNav(can) {
  return (
    can("create_project") || can("manage_providers") || can("view_fleet") || canSeeAccessGroup(can)
  );
}

// Per-item visibility for the 5-item admin nav (command-center/projects/new-project/providers/access).
export function canSeeAdminTab(tabId, can) {
  if (tabId === "command-center") return canSeeAdminNav(can);
  if (tabId === "access") return canSeeAccessGroup(can);
  const perm = ADMIN_NAV_PERMISSIONS[tabId];
  return perm === null || can(perm);
}
