import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { WorkspaceShell } from "./WorkspaceShell.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  streamJobs: vi.fn(() => ({ close: vi.fn() })),
  listEpics: vi.fn(() => Promise.resolve([])),
  streamSuggest: vi.fn(() => ({ cancel: vi.fn() })),
  streamArchitectPlan: vi.fn(() => ({ cancel: vi.fn() })),
  createJob: vi.fn(() => Promise.resolve({})),
  createBatchJobs: vi.fn(() => Promise.resolve({})),
}));

vi.mock("./JobDetailPage.jsx", () => ({
  JobDetailPage: ({ jobId, project, epics, onBack, onOpenJob }) => (
    <div data-testid="job-detail-page" data-job-id={jobId} data-project-id={project?.id}>
      <span data-testid="job-detail-epics-count">{epics.length}</span>
      <button onClick={onBack}>Back</button>
      <button onClick={() => onOpenJob(99)}>open-from-job-detail</button>
    </div>
  ),
}));

vi.mock("./WorkspaceDashboard.jsx", () => ({
  WorkspaceDashboard: () => <div data-testid="dashboard-view" />,
}));
vi.mock("./ProjectDetail.jsx", () => ({
  ProjectDetail: ({ onOpenJob, workState, onWorkStateChange }) => (
    <div data-testid="work-view" data-work-view={workState?.view}>
      <button onClick={() => onOpenJob(99)}>open-from-work</button>
      <button onClick={() => onWorkStateChange?.({ view: "queue" })}>change-work-state</button>
    </div>
  ),
}));
vi.mock("./ProjectsView.jsx", () => ({
  ProjectsView: () => <div data-testid="projects-view" />,
}));
vi.mock("../components/WorkspaceSettings.jsx", () => ({
  WorkspaceSettings: () => <div data-testid="settings-view" />,
}));
vi.mock("./PlanTab.jsx", () => ({
  PlanTab: ({ onOpenJob }) => (
    <div data-testid="plan-view">
      <button onClick={() => onOpenJob(99)}>open-from-plan</button>
    </div>
  ),
}));
vi.mock("./HistoryTab.jsx", () => ({
  HistoryTab: ({ onOpenJob }) => (
    <div data-testid="history-view">
      <button onClick={() => onOpenJob(99)}>open-from-history</button>
    </div>
  ),
}));
vi.mock("./AnalyticsTab.jsx", () => ({
  AnalyticsTab: () => <div data-testid="analytics-view" />,
}));

const PROJECT = { id: 1, name: "Demo", repo_path: "/tmp/demo" };

function renderShell(props = {}) {
  return render(
    <RoleContext.Provider
      value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
    >
      <WorkspaceShell
        projectId={PROJECT.id}
        wsTab="work"
        epicId={null}
        epicTab={null}
        subTab={null}
        jobId={null}
        projects={[PROJECT]}
        projectsLoaded={true}
        onNavWorkspace={vi.fn()}
        onNavEpic={vi.fn()}
        onNavAdmin={vi.fn()}
        onNavJob={vi.fn()}
        onReloadProjects={vi.fn()}
        {...props}
      />
    </RoleContext.Provider>
  );
}

describe("WorkspaceShell job detail route", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders JobDetailPage instead of the normal tab content when jobId is set", async () => {
    renderShell({ jobId: 42 });
    const jobDetail = await screen.findByTestId("job-detail-page");
    expect(jobDetail.getAttribute("data-job-id")).toBe("42");
    expect(jobDetail.getAttribute("data-project-id")).toBe("1");
    expect(screen.queryByTestId("work-view")).not.toBeInTheDocument();
    expect(screen.queryByText("Demo")).not.toBeInTheDocument();
  });

  it("calls onNavWorkspace(currentProjectId, 'work') when JobDetailPage's onBack fires", async () => {
    const onNavWorkspace = vi.fn();
    renderShell({ jobId: 42, onNavWorkspace });
    const backBtn = await screen.findByText("Back");
    backBtn.click();
    expect(onNavWorkspace).toHaveBeenCalledWith(1, "work");
  });

  it("falls back to the normal wsTab rendering when jobId is null", async () => {
    renderShell({ jobId: null });
    expect(await screen.findByTestId("work-view")).toBeInTheDocument();
    expect(screen.queryByTestId("job-detail-page")).not.toBeInTheDocument();
  });
});

describe("WorkspaceShell analytics tab", () => {
  beforeEach(() => vi.clearAllMocks());

  it("renders ProjectsView instead of AnalyticsTab when wsTab is analytics and no project is selected", async () => {
    renderShell({ wsTab: "analytics", projectId: null, jobId: null });
    expect(await screen.findByTestId("projects-view")).toBeInTheDocument();
    expect(screen.queryByTestId("analytics-view")).not.toBeInTheDocument();
  });
});

describe("WorkspaceShell onOpenJob wiring", () => {
  beforeEach(() => vi.clearAllMocks());

  it("passes an onOpenJob function to ProjectDetail that calls onNavJob(currentProjectId, jid)", async () => {
    const onNavJob = vi.fn();
    renderShell({ wsTab: "work", jobId: null, onNavJob });
    const btn = await screen.findByText("open-from-work");
    btn.click();
    expect(onNavJob).toHaveBeenCalledWith(1, 99);
  });

  it("passes an onOpenJob function to PlanTab that calls onNavJob(currentProjectId, jid)", async () => {
    const onNavJob = vi.fn();
    renderShell({ wsTab: "plan", jobId: null, onNavJob });
    const btn = await screen.findByText("open-from-plan");
    btn.click();
    expect(onNavJob).toHaveBeenCalledWith(1, 99);
  });

  it("passes an onOpenJob function to HistoryTab that calls onNavJob(currentProjectId, jid)", async () => {
    const onNavJob = vi.fn();
    renderShell({ wsTab: "history", jobId: null, onNavJob });
    const btn = await screen.findByText("open-from-history");
    btn.click();
    expect(onNavJob).toHaveBeenCalledWith(1, 99);
  });

  it("passes an onOpenJob function to JobDetailPage that calls onNavJob(currentProjectId, jid)", async () => {
    const onNavJob = vi.fn();
    renderShell({ jobId: 42, onNavJob });
    const btn = await screen.findByText("open-from-job-detail");
    btn.click();
    expect(onNavJob).toHaveBeenCalledWith(1, 99);
  });
});

describe("WorkspaceShell Work state wiring", () => {
  it("passes Work state and changes through ProjectDetail", async () => {
    const onWorkStateChange = vi.fn();
    renderShell({
      workState: { view: "history" },
      onWorkStateChange,
    });

    expect(await screen.findByTestId("work-view")).toHaveAttribute("data-work-view", "history");
    screen.getByText("change-work-state").click();
    expect(onWorkStateChange).toHaveBeenCalledWith({ view: "queue" });
  });

  it("uses the routed job return callback when provided", async () => {
    const onBackJob = vi.fn();
    const onNavWorkspace = vi.fn();
    renderShell({ jobId: 42, onBackJob, onNavWorkspace });

    (await screen.findByText("Back")).click();
    expect(onBackJob).toHaveBeenCalledOnce();
    expect(onNavWorkspace).not.toHaveBeenCalled();
  });
});
