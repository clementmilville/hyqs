import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { HistoryTab } from "./HistoryTab.jsx";
import { ToastProvider } from "../components/Toast.jsx";
import { RoleContext } from "../context.js";

vi.mock("./ChangelogTab.jsx", () => ({
  ChangelogTab: () => <div data-testid="changelog-tab" />,
}));
vi.mock("./DecisionsTab.jsx", () => ({
  DecisionsTab: () => <div data-testid="decisions-tab" />,
}));

vi.mock("../api.js", () => ({
  archiveJob: vi.fn(() => Promise.resolve()),
  unarchiveJob: vi.fn(() => Promise.resolve()),
  patchJobEpic: vi.fn(() => Promise.resolve()),
  listJobsFiltered: vi.fn(() => Promise.resolve([])),
  getJobEvents: vi.fn(() => Promise.resolve([])),
  getJobDiff: vi.fn(() =>
    Promise.resolve({ base: "main", branch: "feat", diff: "", truncated: false })
  ),
  streamLogs: vi.fn(() => ({ close: vi.fn() })),
  getJobBacklogSources: vi.fn(() => Promise.resolve([])),
  getJobSupervisorEvents: vi.fn(() => Promise.resolve([])),
  getJobDependents: vi.fn(() => Promise.resolve([])),
  requeueJobAtStage: vi.fn(() => Promise.resolve({})),
  fileFixForwardJob: vi.fn(() => Promise.resolve({ job: { id: 1 } })),
  patchJobIdea: vi.fn(() => Promise.resolve({})),
  resolveJob: vi.fn(() => Promise.resolve({})),
  retryJob: vi.fn(() => Promise.resolve({})),
  setJobPriority: vi.fn(() => Promise.resolve({})),
  getJobDependencies: vi.fn(() => Promise.resolve([])),
  addJobDependency: vi.fn(() => Promise.resolve({})),
  removeJobDependency: vi.fn(() => Promise.resolve({})),
  patchJobTitle: vi.fn(() => Promise.resolve({})),
}));

const project = { id: 1, repo_path: "/repo" };

function renderHistoryTab(props = {}) {
  return render(
    <HistoryTab project={project} subTab="changelog" onNavWorkspace={vi.fn()} {...props} />
  );
}

function renderHistoryTabWithRole(props = {}) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <HistoryTab project={project} subTab="changelog" onNavWorkspace={vi.fn()} {...props} />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("HistoryTab", () => {
  it("renders the Changelog/Decisions sub-tab bar driven by HISTORY_TABS, defaulting to Changelog", () => {
    renderHistoryTab();
    expect(screen.getByText("Changelog")).toBeInTheDocument();
    expect(screen.getByText("Decisions")).toBeInTheDocument();
    expect(screen.getByTestId("changelog-tab")).toBeInTheDocument();
  });

  it("renders DecisionsTab when subTab is 'decisions'", () => {
    renderHistoryTab({ subTab: "decisions" });
    expect(screen.getByTestId("decisions-tab")).toBeInTheDocument();
  });

  it("clicking a sub-tab calls onNavWorkspace with the history sub-tab", () => {
    const onNavWorkspace = vi.fn();
    renderHistoryTab({ onNavWorkspace });
    fireEvent.click(screen.getByText("Decisions"));
    expect(onNavWorkspace).toHaveBeenCalledWith(project.id, "history", "decisions");
  });

  it("renders the Archived sub-tab in the tab bar", () => {
    renderHistoryTab();
    expect(screen.getByText("Archived")).toBeInTheDocument();
  });
});

describe("HistoryTab archived sub-tab forwards onOpenJob", () => {
  it("passes the onOpenJob prop through to JobTable", async () => {
    vi.resetModules();
    vi.doMock("../components/JobTable.jsx", () => ({
      JobTable: (props) => (
        <div
          data-testid="mock-job-table"
          data-has-onopenjob={String(props.onOpenJob === onOpenJob)}
        />
      ),
    }));

    const onOpenJob = vi.fn();
    const { HistoryTab: RemockedHistoryTab } = await import("./HistoryTab.jsx");
    render(
      <RemockedHistoryTab
        project={project}
        subTab="archived"
        onNavWorkspace={vi.fn()}
        onOpenJob={onOpenJob}
      />
    );

    expect(screen.getByTestId("mock-job-table").dataset.hasOnopenjob).toBe("true");

    vi.doUnmock("../components/JobTable.jsx");
    vi.resetModules();
  });
});

describe("HistoryTab archived sub-tab", () => {
  it("fetches archived jobs via listJobsFiltered and opens one via onOpenJob", async () => {
    const api = await import("../api.js");
    const archivedJob = {
      id: 42,
      project_id: 1,
      epic_id: null,
      title: "Archived job",
      status: "cancelled",
      stage: "done",
      priority: 0,
      source: "ui",
      archived: true,
      updated_at: new Date().toISOString(),
    };
    api.listJobsFiltered.mockResolvedValue([archivedJob]);
    const onOpenJob = vi.fn();

    renderHistoryTabWithRole({ subTab: "archived", epics: [], jobs: [], onOpenJob });

    expect(await screen.findByText("Archived job")).toBeInTheDocument();
    expect(api.listJobsFiltered).toHaveBeenCalledWith("archived", project.id);

    fireEvent.click(screen.getByText("Archived job"));

    expect(onOpenJob).toHaveBeenCalledWith(42);
  });
});
