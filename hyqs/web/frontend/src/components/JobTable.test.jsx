import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { JobTable } from "./JobTable.jsx";
import { ToastProvider } from "./Toast.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  archiveJob: vi.fn(() => Promise.resolve()),
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
  unarchiveJob: vi.fn(() => Promise.resolve({})),
}));

const project = { id: 1, repo_path: "/repo" };
const epics = [{ id: 9, name: "Checkout revamp" }];

function makeJob(overrides) {
  return {
    id: 1,
    project_id: 1,
    epic_id: null,
    title: "Job",
    status: "done",
    stage: "done",
    priority: 0,
    source: "ui",
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

function renderTable(props = {}) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <JobTable
          jobs={[]}
          epics={epics}
          project={project}
          selectedEpicId={null}
          onNewJob={vi.fn()}
          onChanged={vi.fn()}
          {...props}
        />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("JobTable default filter and ordering", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([]);
  });

  it("defaults the status filter to All and fetches unfiltered jobs", async () => {
    const api = await import("../api.js");
    const doneOnly = [makeJob({ id: 1, title: "Done job", status: "done" })];
    api.listJobsFiltered.mockResolvedValue(doneOnly);

    renderTable();

    expect(await screen.findByText("Done job")).toBeInTheDocument();
    expect(api.listJobsFiltered).toHaveBeenCalledWith("all", project.id);
    expect(screen.getByText("All").className).toMatch(/active/);
  });

  it("orders jobs attention-first: failed, running, pending, done, cancelled", async () => {
    const api = await import("../api.js");
    const jobs = [
      makeJob({ id: 1, title: "Done job", status: "done" }),
      makeJob({ id: 2, title: "Cancelled job", status: "cancelled" }),
      makeJob({ id: 3, title: "Failed job", status: "failed" }),
      makeJob({ id: 4, title: "Running job", status: "running" }),
      makeJob({ id: 5, title: "Pending job", status: "pending" }),
    ];
    api.listJobsFiltered.mockResolvedValue(jobs);

    renderTable();

    await screen.findByText("Failed job");
    const rows = screen.getAllByRole("row").slice(1);
    const order = rows.map((r) => r.textContent);
    const idx = (needle) => order.findIndex((t) => t.includes(needle));

    expect(idx("Failed job")).toBeLessThan(idx("Running job"));
    expect(idx("Running job")).toBeLessThan(idx("Pending job"));
    expect(idx("Pending job")).toBeLessThan(idx("Done job"));
    expect(idx("Done job")).toBeLessThan(idx("Cancelled job"));
  });

  it("an explicit status chip click overrides the default ordering/filter", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Done job", status: "done" })]);

    renderTable();
    await screen.findByText("Done job");

    fireEvent.click(screen.getByText("Failed"));
    expect(api.listJobsFiltered).toHaveBeenCalledWith("failed", project.id);
  });

  it("clicking a column header overrides the default attention sort", async () => {
    const api = await import("../api.js");
    const jobs = [
      makeJob({ id: 1, title: "B job", status: "failed" }),
      makeJob({ id: 2, title: "A job", status: "done" }),
    ];
    api.listJobsFiltered.mockResolvedValue(jobs);

    renderTable();
    await screen.findByText("B job");

    fireEvent.click(screen.getByText("Title"));
    const rows = screen.getAllByRole("row").slice(1);
    const order = rows.map((r) => r.textContent);
    expect(order[0]).toContain("A job");
    expect(order[1]).toContain("B job");
  });
});

describe("JobTable epic chip", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([]);
  });

  it("renders the epic name as a clickable chip and fires onOpenEpic with its id", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job", epic_id: 9 })]);
    const onOpenEpic = vi.fn();

    renderTable({ onOpenEpic });
    const chip = await screen.findByRole("button", { name: "Checkout revamp" });

    fireEvent.click(chip);
    expect(onOpenEpic).toHaveBeenCalledWith(9);
  });

  it("falls back to plain text when onOpenEpic is not provided", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job", epic_id: 9 })]);

    renderTable();
    await screen.findByText("Job");
    expect(screen.queryByRole("button", { name: "Checkout revamp" })).not.toBeInTheDocument();
    expect(screen.getByText("Checkout revamp")).toBeInTheDocument();
  });
});

describe("JobTable mobile card attributes", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([]);
  });

  it("renders every visible column's data cell with a data-label matching its column label, except Title", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([
      makeJob({ id: 1, title: "Job", epic_id: 9, status: "failed" }),
    ]);

    renderTable();
    await screen.findByText("Job");

    const row = screen.getByText("Job").closest("tr");
    const labeled = [...row.querySelectorAll("[data-label]")].map((el) =>
      el.getAttribute("data-label")
    );

    expect(labeled).toEqual(
      expect.arrayContaining([
        "Select",
        "#",
        "Status",
        "Stage",
        "Priority",
        "Epic",
        "Channel",
        "Actor",
        "Executor",
        "Updated",
      ])
    );
    const titleCell = row.querySelector(".job-table-td-title");
    expect(titleCell.hasAttribute("data-label")).toBe(false);
  });

  it("clicking the checkbox selects the row without calling onOpenJob", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job" })]);
    const onOpenJob = vi.fn();

    renderTable({ onOpenJob });
    await screen.findByText("Job");

    const checkbox = screen.getAllByRole("checkbox")[1];
    fireEvent.click(checkbox);

    expect(checkbox.checked).toBe(true);
    expect(onOpenJob).not.toHaveBeenCalled();
  });

  it("clicking a row calls onOpenJob with the job's id", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job" })]);
    const onOpenJob = vi.fn();

    renderTable({ onOpenJob });
    const titleCell = await screen.findByText("Job");
    const row = titleCell.closest("tr");

    fireEvent.click(row);
    expect(onOpenJob).toHaveBeenCalledWith(1);
  });
});

describe("JobTable inline expand accordion", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([]);
  });

  it("clicking the chevron expands the row to show JobDetailTabs without calling onOpenJob", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job" })]);
    const onOpenJob = vi.fn();

    renderTable({ onOpenJob });
    await screen.findByText("Job");

    const toggle = screen.getByRole("button", { expanded: false });
    fireEvent.click(toggle);

    expect(await screen.findByText("OVERVIEW")).toBeInTheDocument();
    expect(onOpenJob).not.toHaveBeenCalled();
  });

  it("clicking the row body still calls onOpenJob unaffected by the new column", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job" })]);
    const onOpenJob = vi.fn();

    renderTable({ onOpenJob });
    const titleCell = await screen.findByText("Job");

    fireEvent.click(titleCell);
    expect(onOpenJob).toHaveBeenCalledWith(1);
  });

  it("clicking the chevron a second time collapses the row again", async () => {
    const api = await import("../api.js");
    api.listJobsFiltered.mockResolvedValue([makeJob({ id: 1, title: "Job" })]);

    renderTable();
    await screen.findByText("Job");

    const toggle = screen.getByRole("button", { expanded: false });
    fireEvent.click(toggle);
    expect(await screen.findByText("OVERVIEW")).toBeInTheDocument();

    fireEvent.click(toggle);
    expect(screen.queryByText("OVERVIEW")).not.toBeInTheDocument();
  });
});
