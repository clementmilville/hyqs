import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { WorkLaneSection } from "./WorkLaneSection.jsx";
import { COLUMNS } from "./jobTableShared.jsx";

vi.mock("../api.js", () => ({
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

const epicsById = {};

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

function renderSection(props = {}) {
  return render(
    <WorkLaneSection
      title="Lane"
      jobs={[]}
      epicsById={epicsById}
      visibleCols={COLUMNS}
      onOpenEpic={vi.fn()}
      onOpenJob={vi.fn()}
      {...props}
    />
  );
}

describe("WorkLaneSection shared worklist selection", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("selects a desktop row without rendering inline detail tabs", async () => {
    const onOpenJob = vi.fn();
    const onSelectJob = vi.fn();
    renderSection({ jobs: [makeJob({ id: 1, title: "Job" })], onOpenJob, onSelectJob });

    fireEvent.click(await screen.findByText("Job"));

    expect(onSelectJob).toHaveBeenCalledWith(1);
    expect(onOpenJob).not.toHaveBeenCalled();
    expect(screen.queryByText("OVERVIEW")).not.toBeInTheDocument();
  });

  it("opens the canonical job page from a mobile card", async () => {
    const onOpenJob = vi.fn();
    renderSection({ jobs: [makeJob({ id: 1, title: "Job" })], onOpenJob, mobile: true });

    const mobileTitle = (await screen.findAllByText("Job")).at(-1);
    fireEvent.click(mobileTitle);

    expect(onOpenJob).toHaveBeenCalledWith(1);
  });

  it("marks the selected desktop row", async () => {
    renderSection({ jobs: [makeJob({ id: 1, title: "Job" })], selectedJobId: 1 });

    const rows = screen.getAllByRole("row");
    expect(rows.some((row) => row.classList.contains("selected"))).toBe(true);
  });

  it("associates its toggle with all lane content and updates accessible state", () => {
    renderSection({
      jobs: [makeJob({ title: "Controlled job" })],
      collapsible: true,
      aboveTable: <div>Auxiliary content</div>,
    });

    const toggle = screen.getByRole("button", { name: /Lane/ });
    const contentId = toggle.getAttribute("aria-controls");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(document.getElementById(contentId)).toContainElement(
      screen.getByText("Auxiliary content")
    );
    expect(document.getElementById(contentId)).toContainElement(screen.getByText("Controlled job"));

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(document.getElementById(contentId)).not.toBeInTheDocument();
    expect(screen.queryByText("Auxiliary content")).not.toBeInTheDocument();
    expect(screen.queryByText("Controlled job")).not.toBeInTheDocument();

    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Auxiliary content")).toBeInTheDocument();
    expect(screen.getByText("Controlled job")).toBeInTheDocument();
  });

  it("keeps empty sections collapsed and non-expandable", () => {
    renderSection({ collapsible: true });

    const header = screen.getByRole("button", { name: /Lane/ });
    expect(header).toBeDisabled();
    expect(header).not.toHaveAttribute("aria-expanded");
    expect(header).not.toHaveAttribute("aria-controls");
  });

  it("honors changed defaults only until the user toggles", () => {
    const job = makeJob({ title: "Persistent choice" });
    const view = renderSection({ jobs: [job], collapsible: true, defaultCollapsed: true });
    const toggle = screen.getByRole("button", { name: /Lane/ });
    expect(toggle).toHaveAttribute("aria-expanded", "false");

    view.rerender(
      <WorkLaneSection
        title="Lane"
        jobs={[job]}
        epicsById={epicsById}
        visibleCols={COLUMNS}
        collapsible
        defaultCollapsed={false}
      />
    );
    expect(toggle).toHaveAttribute("aria-expanded", "true");

    fireEvent.click(toggle);
    view.rerender(
      <WorkLaneSection
        title="Lane"
        jobs={[job]}
        epicsById={epicsById}
        visibleCols={COLUMNS}
        collapsible
        defaultCollapsed={false}
      />
    );
    expect(toggle).toHaveAttribute("aria-expanded", "false");
  });
});
