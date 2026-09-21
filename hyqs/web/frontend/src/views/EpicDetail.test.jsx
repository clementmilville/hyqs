import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EpicDetail } from "./EpicDetail.jsx";
import { ToastProvider } from "../components/Toast.jsx";
import { RoleContext } from "../context.js";

const epic = {
  id: 7,
  name: "Checkout revamp",
  status: "active",
  archived: false,
  job_count: 2,
  last_activity: new Date().toISOString(),
};

const project = { id: 1, repo_path: "/repo" };

const jobs = [
  {
    id: 101,
    project_id: 1,
    epic_id: 7,
    title: "Build cart",
    status: "done",
    stage: "done",
    priority: 0,
    source: "ui",
    updated_at: new Date().toISOString(),
  },
  {
    id: 102,
    project_id: 1,
    epic_id: 7,
    title: "Add coupons",
    status: "pending",
    stage: "queued",
    priority: 0,
    source: "ui",
    updated_at: new Date().toISOString(),
  },
];

vi.mock("../api.js", () => ({
  archiveEpic: vi.fn(() => Promise.resolve()),
  unarchiveEpic: vi.fn(() => Promise.resolve()),
  archiveJob: vi.fn(() => Promise.resolve()),
  patchJobEpic: vi.fn(() => Promise.resolve()),
  // JobTable now defaults its status filter to "all", so it fetches through
  // listJobsFiltered on mount rather than using the "active" jobs prop.
  listJobsFiltered: vi.fn(() => Promise.resolve(jobs)),
}));

function emptyIdeate(overrides = {}) {
  return {
    panel: null,
    streaming: false,
    text: "",
    toolEvents: [],
    open: vi.fn(),
    stop: vi.fn(),
    toggleChecked: vi.fn(),
    createSelected: vi.fn(),
    dismiss: vi.fn(),
    ...overrides,
  };
}

function emptyArchitect(overrides = {}) {
  return {
    panel: null,
    streaming: false,
    text: "",
    toolEvents: [],
    open: vi.fn(),
    stop: vi.fn(),
    toggleChecked: vi.fn(),
    createSelected: vi.fn(),
    dismiss: vi.fn(),
    ...overrides,
  };
}

function renderDetail(props = {}) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <EpicDetail
          epic={epic}
          project={project}
          jobs={jobs}
          epics={[epic]}
          tab="jobs"
          onTabChange={vi.fn()}
          onNewJob={vi.fn()}
          ideate={emptyIdeate()}
          architect={emptyArchitect()}
          onChanged={vi.fn()}
          setNote={vi.fn()}
          {...props}
        />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("EpicDetail tabs", () => {
  beforeEach(() => vi.clearAllMocks());

  it("does not render a pseudo back link — breadcrumb navigation replaces it", () => {
    renderDetail({ tab: "jobs" });
    expect(screen.queryByText("← Back")).not.toBeInTheDocument();
  });

  it("JOBS tab reuses JobTable and shows only this epic's jobs", async () => {
    renderDetail({ tab: "jobs" });
    expect(await screen.findByText("Build cart")).toBeInTheDocument();
    expect(screen.getByText("Add coupons")).toBeInTheDocument();
  });

  it("shows an epic's done jobs immediately with no running/pending jobs, ordered attention-first", async () => {
    const doneHeavyJobs = [
      {
        id: 201,
        project_id: 1,
        epic_id: 7,
        title: "Old done job",
        status: "done",
        stage: "done",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
      {
        id: 202,
        project_id: 1,
        epic_id: 7,
        title: "Broken job",
        status: "failed",
        stage: "build",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
    ];
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValueOnce(doneHeavyJobs);

    renderDetail({ tab: "jobs", jobs: doneHeavyJobs });

    // Visible without any user click on a status chip.
    expect(await screen.findByText("Old done job")).toBeInTheDocument();
    expect(screen.getByText("Broken job")).toBeInTheDocument();

    const rows = screen.getAllByRole("row").slice(1); // drop header row
    const titles = rows.map((r) => r.textContent);
    // failed sorts before done.
    expect(titles.findIndex((t) => t.includes("Broken job"))).toBeLessThan(
      titles.findIndex((t) => t.includes("Old done job"))
    );
  });

  it("switching tabs does not reset ideate stream state carried in props", async () => {
    const ideate = emptyIdeate({
      panel: { epicId: 7, suggestions: [], checked: {}, done: false },
      streaming: true,
      text: "thinking about features…",
    });
    const { rerender } = renderDetail({ tab: "ideate", ideate });
    expect(screen.getByText("thinking about features…")).toBeInTheDocument();

    rerender(
      <ToastProvider>
        <RoleContext.Provider
          value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
        >
          <EpicDetail
            epic={epic}
            project={project}
            jobs={jobs}
            epics={[epic]}
            tab="jobs"
            onTabChange={vi.fn()}
            onNewJob={vi.fn()}
            ideate={ideate}
            architect={emptyArchitect()}
            onChanged={vi.fn()}
            setNote={vi.fn()}
          />
        </RoleContext.Provider>
      </ToastProvider>
    );
    expect(await screen.findByText("Build cart")).toBeInTheDocument();

    rerender(
      <ToastProvider>
        <RoleContext.Provider
          value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
        >
          <EpicDetail
            epic={epic}
            project={project}
            jobs={jobs}
            epics={[epic]}
            tab="ideate"
            onTabChange={vi.fn()}
            onNewJob={vi.fn()}
            ideate={ideate}
            architect={emptyArchitect()}
            onChanged={vi.fn()}
            setNote={vi.fn()}
          />
        </RoleContext.Provider>
      </ToastProvider>
    );
    // Same in-progress stream text is still visible — it lives in a prop
    // owned by a component higher in the tree, not local EpicDetail state.
    expect(screen.getByText("thinking about features…")).toBeInTheDocument();
  });

  it("ARCHITECT tab groups jobs by dependency depth and shows dep/scope chips", () => {
    const architect = emptyArchitect({
      panel: {
        epicId: 7,
        done: true,
        summary: "Ship it in two steps",
        checked: {},
        jobs: [
          {
            title: "Job A",
            idea: "Do A",
            depends_on: [],
            scope: { allowed_paths: ["a.py", "b.py"] },
          },
          { title: "Job B", idea: "Do B", depends_on: [0] },
        ],
      },
    });
    renderDetail({ tab: "architect", architect });

    expect(screen.getByText("Step 1")).toBeInTheDocument();
    expect(screen.getByText("Step 2")).toBeInTheDocument();
    expect(screen.getByText(/depends on: Job A/)).toBeInTheDocument();
    expect(screen.getByText("2 files")).toBeInTheDocument();
  });

  it("IDEATE and ARCHITECT empty-tab CTAs render a lucide icon plus their text label, no emoji", () => {
    const { container: ideateContainer } = renderDetail({ tab: "ideate" });
    expect(screen.getByText("Suggest features")).toBeInTheDocument();
    expect(ideateContainer.querySelector("button svg")).toBeInTheDocument();
    expect(ideateContainer.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}☀-➿]/u);

    const { container: architectContainer } = renderDetail({ tab: "architect" });
    expect(screen.getByText("Architect this epic")).toBeInTheDocument();
    expect(architectContainer.querySelector("button svg")).toBeInTheDocument();
    expect(architectContainer.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}☀-➿]/u);
  });

  it("JOBS tab defaults to attention-first: failed/running jobs are visible, never hidden behind an empty/done-only default", async () => {
    const mixedJobs = [
      {
        id: 301,
        project_id: 1,
        epic_id: 7,
        title: "Finished job",
        status: "done",
        stage: "done",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
      {
        id: 302,
        project_id: 1,
        epic_id: 7,
        title: "Running job",
        status: "running",
        stage: "build",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
      {
        id: 303,
        project_id: 1,
        epic_id: 7,
        title: "Failed job",
        status: "failed",
        stage: "test",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
      {
        id: 304,
        project_id: 1,
        epic_id: 7,
        title: "Deploying job",
        status: "deploying",
        stage: "deploy",
        priority: 0,
        source: "ui",
        updated_at: new Date().toISOString(),
      },
    ];
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValueOnce(mixedJobs);

    renderDetail({ tab: "jobs", jobs: mixedJobs });

    expect(await screen.findByText("Failed job")).toBeInTheDocument();
    expect(screen.getByText("Running job")).toBeInTheDocument();
    expect(screen.getByText("Deploying job")).toBeInTheDocument();
    expect(screen.getByText("Finished job")).toBeInTheDocument();
    expect(screen.getByText("deploying")).toHaveClass("badge", "run");

    const rows = screen.getAllByRole("row").slice(1);
    const titles = rows.map((r) => r.textContent);
    expect(titles.findIndex((t) => t.includes("Failed job"))).toBeLessThan(
      titles.findIndex((t) => t.includes("Finished job"))
    );
    expect(titles.findIndex((t) => t.includes("Running job"))).toBeLessThan(
      titles.findIndex((t) => t.includes("Finished job"))
    );
    expect(titles.findIndex((t) => t.includes("Deploying job"))).toBeLessThan(
      titles.findIndex((t) => t.includes("Finished job"))
    );
  });

  it("create-selected on the IDEATE tab invokes the passed createSelected callback", () => {
    const createSelected = vi.fn();
    const ideate = emptyIdeate({
      panel: {
        epicId: 7,
        done: true,
        checked: { 0: true },
        suggestions: [{ title: "Suggestion A", description: "Do A" }],
      },
      createSelected,
    });
    renderDetail({ tab: "ideate", ideate });

    fireEvent.click(screen.getByRole("button", { name: "Create selected" }));
    expect(createSelected).toHaveBeenCalledWith(epic);
  });

  it("create-selected on the ARCHITECT tab invokes the passed createSelected callback", () => {
    const createSelected = vi.fn();
    const architect = emptyArchitect({
      panel: {
        epicId: 7,
        done: true,
        checked: { 0: true },
        jobs: [{ title: "Job A", idea: "Do A", depends_on: [] }],
      },
      createSelected,
    });
    renderDetail({ tab: "architect", architect });

    fireEvent.click(screen.getByRole("button", { name: "Create selected" }));
    expect(createSelected).toHaveBeenCalledWith(epic);
  });

  it("JOBS tab forwards onOpenJob through to JobTable", async () => {
    vi.resetModules();
    let receivedProps;
    vi.doMock("../components/JobTable.jsx", () => ({
      JobTable: (props) => {
        receivedProps = props;
        return <div data-testid="mock-job-table" />;
      },
    }));

    const { EpicDetail: MockedEpicDetail } = await import("./EpicDetail.jsx");
    const onOpenJob = vi.fn();

    render(
      <ToastProvider>
        <RoleContext.Provider
          value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
        >
          <MockedEpicDetail
            epic={epic}
            project={project}
            jobs={jobs}
            epics={[epic]}
            tab="jobs"
            onTabChange={vi.fn()}
            onNewJob={vi.fn()}
            onOpenJob={onOpenJob}
            ideate={emptyIdeate()}
            architect={emptyArchitect()}
            onChanged={vi.fn()}
            setNote={vi.fn()}
          />
        </RoleContext.Provider>
      </ToastProvider>
    );

    expect(await screen.findByTestId("mock-job-table")).toBeInTheDocument();
    expect(receivedProps.onOpenJob).toBe(onOpenJob);

    vi.doUnmock("../components/JobTable.jsx");
    vi.resetModules();
  });
});
