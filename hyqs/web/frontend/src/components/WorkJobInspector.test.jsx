import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { getJobDiff, getJobEvents } from "../api.js";
import { WorkJobInspector } from "./WorkJobInspector.jsx";

vi.mock("../api.js", () => ({
  getJobEvents: vi.fn(() => Promise.resolve([])),
  getJobDiff: vi.fn(() =>
    Promise.resolve({ base: "main", branch: "feature", diff: "", truncated: false })
  ),
  streamLogs: vi.fn(() => ({ close: vi.fn() })),
  getJobBacklogSources: vi.fn(() => Promise.resolve([])),
  getJobSupervisorEvents: vi.fn(() => Promise.resolve([])),
  getJobDependents: vi.fn(() => Promise.resolve([])),
  getJobDependencyJobs: vi.fn(() => Promise.resolve([])),
  getJobUsage: vi.fn(() => Promise.resolve([])),
  requeueJobAtStage: vi.fn(() => Promise.resolve({})),
  fileFixForwardJob: vi.fn(() => Promise.resolve({ job: { id: 1 } })),
  patchJobIdea: vi.fn(() => Promise.resolve({})),
  resolveJob: vi.fn(() => Promise.resolve({})),
  retryJob: vi.fn(() => Promise.resolve({})),
  archiveJob: vi.fn(() => Promise.resolve({})),
  updateProject: vi.fn(() => Promise.resolve({})),
}));

const JOB = {
  id: 42,
  title: "Improve the Work inspector",
  status: "running",
  source: "slack",
  source_actor: "sam@example.com",
  next_action: "Verify the focused tests",
  current_executor: { label: "Codex builder", provider: "openai" },
};
const PROJECT = { id: 7, max_fix_attempts: null };
const EVENTS = [
  {
    id: 1,
    stage: "plan",
    status: "done",
    agent_name: "Planner",
    agent_provider: "anthropic",
    detail: {},
  },
];

describe("WorkJobInspector", () => {
  beforeEach(() => {
    Object.defineProperty(window, "innerWidth", {
      configurable: true,
      writable: true,
      value: 1600,
    });
    window.PointerEvent = MouseEvent;
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps its heading and dismissal control outside the dedicated content scroller", () => {
    render(<WorkJobInspector job={JOB} project={PROJECT} events={EVENTS} />);

    const inspector = screen.getByRole("complementary");
    const content = within(inspector).getByRole("region", {
      name: "Job #42 inspector content",
    });
    const heading = within(inspector).getByRole("heading", {
      name: "Job #42 Improve the Work inspector",
    });
    const close = within(inspector).getByRole("button", { name: "Close inspector" });

    expect(inspector).toHaveClass("work-job-inspector-desktop");
    expect(content).toHaveClass("work-job-inspector-content");
    expect(content).not.toContainElement(heading);
    expect(content).not.toContainElement(close);
    expect(heading.closest("header")).toHaveClass("work-job-inspector-header");
  });

  it("uses an adaptive desktop width and viewport-derived resize bounds", () => {
    render(<WorkJobInspector job={JOB} project={PROJECT} />);

    const inspector = screen.getByRole("complementary");
    const separator = screen.getByRole("separator", { name: "Resize job inspector" });
    expect(inspector).toHaveStyle({ width: "720px" });
    expect(separator).toHaveAttribute("aria-valuemin", "360");
    expect(separator).toHaveAttribute("aria-valuemax", "1100");
    expect(separator).toHaveAttribute("aria-valuenow", "720");
    expect(separator).toHaveAttribute("aria-orientation", "vertical");
    expect(separator).toHaveAttribute("title", "Resize job inspector");
    expect(separator).toHaveClass("work-job-inspector-resize");
  });

  it("clamps adaptive sizing and the active width when the viewport changes", () => {
    window.innerWidth = 800;
    render(<WorkJobInspector job={JOB} project={PROJECT} initialWidth={1000} />);

    const inspector = screen.getByRole("complementary");
    const separator = screen.getByRole("separator", { name: "Resize job inspector" });
    expect(inspector).toHaveStyle({ width: "640px" });
    expect(separator).toHaveAttribute("aria-valuemax", "640");

    window.innerWidth = 500;
    fireEvent(window, new Event("resize"));
    expect(inspector).toHaveStyle({ width: "400px" });
    expect(separator).toHaveAttribute("aria-valuemin", "360");
    expect(separator).toHaveAttribute("aria-valuemax", "400");
    expect(separator).toHaveAttribute("aria-valuenow", "400");
  });

  it("separates operational context, historical execution, and remediation lineage", () => {
    render(
      <WorkJobInspector
        job={JOB}
        project={PROJECT}
        events={EVENTS}
        workerContext={{ id: "worker-3", status: "busy", host: "build-1" }}
        lineage={[{ relationship: "fixes", job_id: 31, title: "Original failure" }]}
      />
    );

    expect(
      within(screen.getByRole("region", { name: "Current executor" })).getByText("Codex builder")
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Prior stage agents and providers" })).getByText(
        /Planner · anthropic/
      )
    ).toBeInTheDocument();
    expect(
      within(screen.getByRole("region", { name: "Remediation lineage" })).getByText(
        /fixes · job #31/
      )
    ).toBeInTheDocument();
    expect(screen.getByText("slack · sam@example.com")).toBeInTheDocument();
    expect(screen.getByText("worker-3 · busy · build-1")).toBeInTheDocument();
  });

  it("resizes the desktop side panel with pointer and keyboard controls within its bounds", () => {
    render(<WorkJobInspector job={JOB} project={PROJECT} initialWidth={400} />);
    const inspector = screen.getByRole("complementary");
    const separator = screen.getByRole("separator", { name: "Resize job inspector" });

    fireEvent.pointerDown(separator, { pointerId: 1, clientX: 500 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 400 });
    expect(inspector).toHaveStyle({ width: "500px" });
    fireEvent.pointerCancel(separator, { pointerId: 1 });
    fireEvent.pointerMove(separator, { pointerId: 1, clientX: 200 });
    expect(inspector).toHaveStyle({ width: "500px" });
    fireEvent.pointerDown(separator, { pointerId: 2, clientX: 400 });
    fireEvent.pointerMove(separator, { pointerId: 2, clientX: 350 });
    fireEvent.pointerUp(separator, { pointerId: 2, clientX: 350 });
    fireEvent.pointerMove(separator, { pointerId: 2, clientX: 250 });
    expect(inspector).toHaveStyle({ width: "550px" });

    fireEvent.keyDown(separator, { key: "ArrowLeft" });
    expect(inspector).toHaveStyle({ width: "570px" });
    for (let index = 0; index < 30; index += 1) fireEvent.keyDown(separator, { key: "ArrowRight" });
    expect(inspector).toHaveStyle({ width: "360px" });
  });

  it("opens full job details from the visible heading control", () => {
    const onOpenJob = vi.fn();
    render(<WorkJobInspector job={JOB} project={PROJECT} onOpenJob={onOpenJob} />);
    const headingControl = screen.getByRole("button", {
      name: "Job #42 Improve the Work inspector",
    });

    fireEvent.click(headingControl);
    expect(onOpenJob).toHaveBeenCalledWith(42);
    headingControl.focus();
    fireEvent.keyDown(headingControl, { key: "Enter" });
    expect(onOpenJob).toHaveBeenCalledTimes(2);
    expect(onOpenJob).toHaveBeenLastCalledWith(42);
  });

  it("traps tablet focus, closes on Escape, and returns focus to the opener", () => {
    const onClose = vi.fn();
    const opener = document.createElement("button");
    opener.textContent = "Open";
    document.body.append(opener);
    opener.focus();
    const openerRef = { current: opener };
    const { unmount } = render(
      <WorkJobInspector
        job={JOB}
        project={PROJECT}
        mode="tablet"
        openerRef={openerRef}
        onClose={onClose}
      />
    );
    const dialog = screen.getByRole("dialog");
    const close = screen.getByRole("button", { name: "Close inspector" });
    expect(close).toHaveFocus();
    expect(screen.queryByRole("separator")).not.toBeInTheDocument();
    expect(dialog).toHaveClass("work-job-inspector-tablet");
    expect(dialog).not.toHaveAttribute("style");

    const buttons = within(dialog).getAllByRole("button");
    buttons.at(-1).focus();
    fireEvent.keyDown(buttons.at(-1), { key: "Tab" });
    expect(close).toHaveFocus();
    fireEvent.keyDown(close, { key: "Tab", shiftKey: true });
    expect(buttons.at(-1)).toHaveFocus();

    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
    unmount();
    expect(opener).toHaveFocus();
    opener.remove();
  });

  it("closes from its explicit dismissal control", () => {
    const onClose = vi.fn();
    render(<WorkJobInspector job={JOB} project={PROJECT} mode="tablet" onClose={onClose} />);
    fireEvent.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not fetch or mount heavy content until a tab is selected", () => {
    getJobEvents.mockClear();
    getJobDiff.mockClear();
    render(<WorkJobInspector job={JOB} project={PROJECT} events={EVENTS} />);

    expect(getJobEvents).not.toHaveBeenCalled();
    expect(getJobDiff).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("DIFF"));
    expect(getJobDiff).toHaveBeenCalledWith(JOB.id);
  });
});
