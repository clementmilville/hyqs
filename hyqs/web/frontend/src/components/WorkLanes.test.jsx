import { beforeEach, describe, expect, it, vi } from "vitest";
import { useState } from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { WorkLanes } from "./WorkLanes.jsx";
import { ToastProvider } from "./Toast.jsx";
import { RoleContext } from "../context.js";

vi.mock("../hooks/useFleetData.js", () => ({ useFleetData: vi.fn() }));

const project = { id: 1, repo_path: "/repo" };
const epics = [{ id: 9, name: "Checkout revamp" }];
const emptyFleet = {
  fleetData: { workers: [], pauses: [], merge_locks: [] },
  supData: { needs_judgment: [], dead_lettered: [] },
  forbidden: false,
  error: false,
  lastFleetAt: Date.now(),
  retry: vi.fn(),
};

function makeJob(overrides) {
  return {
    id: 1,
    project_id: 1,
    epic_id: null,
    title: "Job",
    status: "pending",
    stage: "queued",
    priority: 0,
    source: "ui",
    updated_at: "2026-07-28T12:00:00Z",
    ...overrides,
  };
}

function mockMobileViewport() {
  const original = window.matchMedia;
  window.matchMedia = vi.fn((query) => ({
    matches: query === "(max-width: 600px)",
    media: query,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  }));
  return () => {
    window.matchMedia = original;
  };
}

function renderLanes(props = {}) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <WorkLanes
          project={project}
          epics={epics}
          jobs={[]}
          onNewJob={vi.fn()}
          onOpenEpic={vi.fn()}
          onOpenJob={vi.fn()}
          {...props}
        />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

function StatefulWorkLanes({ initialState, jobs }) {
  const [workState, setWorkState] = useState(initialState);
  return (
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <WorkLanes
          project={project}
          epics={epics}
          jobs={jobs}
          onNewJob={vi.fn()}
          onOpenEpic={vi.fn()}
          onOpenJob={vi.fn()}
          workState={workState}
          onWorkStateChange={setWorkState}
        />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("WorkLanes", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    localStorage.clear();
    const { useFleetData } = await import("../hooks/useFleetData.js");
    useFleetData.mockReturnValue(emptyFleet);
  });

  it("classifies one mixed snapshot exactly once with deploying and blockers distinct", () => {
    const jobs = [
      makeJob({ id: 1, title: "Failed", status: "failed", failure_class: "test" }),
      makeJob({ id: 2, title: "Running", status: "running" }),
      makeJob({ id: 3, title: "Deploying", status: "deploying" }),
      makeJob({ id: 4, title: "Blocked", waiting_on: [11] }),
      makeJob({ id: 5, title: "Ready" }),
      makeJob({ id: 6, title: "Done", status: "done" }),
    ];
    renderLanes({ jobs });

    expect(
      within(screen.getByText("Needs Attention").closest("section")).getByText("Failed")
    ).toBeInTheDocument();
    expect(
      within(screen.getByText("Running Now").closest("section")).getByText("Running")
    ).toBeInTheDocument();
    expect(
      within(
        screen.getByText("Deploying", { selector: ".work-lane-title" }).closest("section")
      ).getByText("Deploying", { selector: ".job-table-title" })
    ).toBeInTheDocument();
    const blocked = screen
      .getByText("Blocked", { selector: ".work-lane-title" })
      .closest("section");
    expect(within(blocked).getByText("waiting on #11")).toBeInTheDocument();
    expect(within(blocked).getByText("Complete #11")).toBeInTheDocument();
    expect(
      within(screen.getByText("Queued").closest("section")).getByText("Ready")
    ).toBeInTheDocument();
    expect(screen.getAllByText("Failed")).toHaveLength(1);
    expect(screen.getAllByText("Running")).toHaveLength(1);
    expect(screen.getAllByText("Deploying")).toHaveLength(2);
    expect(screen.getAllByText("Ready")).toHaveLength(1);
  });

  it("distinguishes ready and waiting queue work and retains epic grouping", () => {
    const jobs = [
      makeJob({ id: 1, title: "Ready checkout", epic_id: 9 }),
      makeJob({ id: 2, title: "Waiting checkout", epic_id: 9, waiting_on: [1] }),
    ];
    const view = renderLanes({ jobs, workState: { view: "queue", grouping: "status" } });

    expect(
      within(screen.getByText("Ready").closest("section")).getByText("Ready checkout")
    ).toBeInTheDocument();
    expect(
      within(screen.getByText("Waiting").closest("section")).getByText("Waiting checkout")
    ).toBeInTheDocument();

    view.rerender(
      <ToastProvider>
        <RoleContext.Provider value={{ role: "project_admin", can: () => true }}>
          <WorkLanes
            project={project}
            epics={epics}
            jobs={jobs}
            onOpenJob={vi.fn()}
            workState={{ view: "queue", grouping: "epic" }}
          />
        </RoleContext.Provider>
      </ToastProvider>
    );
    const epic = screen
      .getByText("Checkout revamp", { selector: ".work-lane-title" })
      .closest("section");
    expect(within(epic).getByText("Ready checkout")).toBeInTheDocument();
    expect(within(epic).getByText("Waiting checkout")).toBeInTheDocument();
  });

  it("orders unresolved failures before resolved failures, done, and cancelled in history", () => {
    renderLanes({
      workState: { view: "history", grouping: "status" },
      jobs: [
        makeJob({ id: 1, title: "Done", status: "done" }),
        makeJob({ id: 2, title: "Resolved", status: "failed", resolution: "resolved" }),
        makeJob({ id: 3, title: "Cancelled", status: "cancelled" }),
        makeJob({ id: 4, title: "Unresolved", status: "failed" }),
      ],
    });
    const rows = [
      ...screen
        .getByText("History", { selector: ".work-lane-title" })
        .closest("section")
        .querySelectorAll("tbody tr"),
    ];
    expect(rows.map((row) => row.textContent)).toEqual([
      expect.stringContaining("Unresolved"),
      expect.stringContaining("Resolved"),
      expect.stringContaining("Done"),
      expect.stringContaining("Cancelled"),
    ]);
  });

  it("excludes archived jobs from every focus lane while preserving unarchived behavior", () => {
    renderLanes({
      jobs: [
        makeJob({ id: 1, title: "Archived failed", status: "failed", archived: true }),
        makeJob({ id: 2, title: "Archived cancelled", status: "cancelled", archived: true }),
        makeJob({ id: 3, title: "Archived done", status: "done", archived: true }),
        makeJob({ id: 4, title: "Active failed", status: "failed" }),
        makeJob({ id: 5, title: "Active done", status: "done" }),
      ],
    });

    const needsAttention = screen.getByText("Needs Attention").closest("section");
    const recentlyFinished = screen.getByText("Recently Finished").closest("section");
    expect(within(needsAttention).getByText("Active failed")).toBeInTheDocument();
    fireEvent.click(within(recentlyFinished).getByRole("button"));
    expect(within(recentlyFinished).getByText("Active done")).toBeInTheDocument();
    expect(screen.queryByText("Archived failed")).not.toBeInTheDocument();
    expect(screen.queryByText("Archived cancelled")).not.toBeInTheDocument();
    expect(screen.queryByText("Archived done")).not.toBeInTheDocument();
  });

  it("routes archived lifecycle records exactly once to history", () => {
    renderLanes({
      workState: { view: "history", grouping: "status" },
      jobs: [
        makeJob({ id: 1, title: "Archived failed", status: "failed", archived: true }),
        makeJob({ id: 2, title: "Archived cancelled", status: "cancelled", archived: true }),
        makeJob({ id: 3, title: "Archived done", status: "done", archived: true }),
        makeJob({ id: 4, title: "Active failed", status: "failed" }),
        makeJob({ id: 5, title: "Active done", status: "done" }),
      ],
    });

    for (const title of [
      "Archived failed",
      "Archived cancelled",
      "Archived done",
      "Active failed",
      "Active done",
    ]) {
      expect(screen.getAllByText(title)).toHaveLength(1);
    }
  });

  it("uses the snapshot for filters, selected navigation, and compact fleet state", async () => {
    const { useFleetData } = await import("../hooks/useFleetData.js");
    const retry = vi.fn();
    useFleetData.mockReturnValue({
      ...emptyFleet,
      error: true,
      retry,
      fleetData: {
        workers: [{ id: 8, project_id: 1, status: "busy", alive: true }],
        pauses: [{ provider: "codex", until: 2_000_000_000 }],
        merge_locks: [{ repo_path: "/repo", owner: "worker-1" }],
      },
    });
    const onOpenJob = vi.fn();
    renderLanes({
      jobs: [makeJob({ id: 7, title: "Deploy checkout", status: "deploying", epic_id: 9 })],
      onOpenJob,
      workState: { search: "checkout", epicId: 9, selectedJobId: 7 },
    });

    fireEvent.click(screen.getByText(/Open #7/));
    expect(onOpenJob).toHaveBeenCalledWith(7);
    expect(screen.getByText(/Paused:/)).toBeInTheDocument();
    expect(screen.getByText(/Merging:/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Retry"));
    expect(retry).toHaveBeenCalled();
  });

  it("collapses populated focus lanes independently with worker content owned by running", async () => {
    const { useFleetData } = await import("../hooks/useFleetData.js");
    useFleetData.mockReturnValue({
      ...emptyFleet,
      fleetData: {
        workers: [
          {
            id: "worker-8",
            project_id: 1,
            job_id: 2,
            status: "busy",
            alive: true,
            stage: "build",
            provider: "codex",
            idea: "Run checkout",
            busy_for: 5,
          },
        ],
        pauses: [],
        merge_locks: [],
      },
    });
    renderLanes({
      jobs: [
        makeJob({ id: 1, title: "Fix checkout", status: "failed" }),
        makeJob({ id: 2, title: "Run checkout", status: "running" }),
      ],
    });

    const attentionToggle = screen.getByRole("button", { name: /Needs Attention/ });
    const runningToggle = screen.getByRole("button", { name: /Running Now/ });
    expect(attentionToggle).toHaveAttribute("aria-expanded", "true");
    expect(runningToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("worker-8")).toBeInTheDocument();

    fireEvent.click(attentionToggle);
    expect(attentionToggle).toHaveAttribute("aria-expanded", "false");
    expect(runningToggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.queryByText("Fix checkout")).not.toBeInTheDocument();
    expect(screen.getByText("Run checkout", { selector: ".job-table-title" })).toBeInTheDocument();

    fireEvent.click(runningToggle);
    expect(screen.queryByText("worker-8")).not.toBeInTheDocument();
    fireEvent.click(runningToggle);
    expect(screen.getByText("worker-8")).toBeInTheDocument();
    fireEvent.click(attentionToggle);
    expect(screen.getByText("Fix checkout")).toBeInTheDocument();
  });

  it.each([
    ["queue", "Ready", makeJob({ title: "Queued job" })],
    ["history", "History", makeJob({ title: "Historic job", status: "done" })],
  ])("makes the populated %s section toggleable", (viewName, laneName, job) => {
    renderLanes({ jobs: [job], workState: { view: viewName, grouping: "status" } });

    const toggle = screen
      .getAllByRole("button", { name: new RegExp(laneName) })
      .find((button) => button.hasAttribute("aria-expanded"));
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
  });

  it("makes epic groups toggleable without changing the active filter or selection", () => {
    const onWorkStateChange = vi.fn();
    renderLanes({
      jobs: [makeJob({ id: 7, title: "Selected checkout", epic_id: 9, status: "running" })],
      workState: {
        view: "focus",
        grouping: "epic",
        epicId: 9,
        search: "checkout",
        selectedJobId: 7,
      },
      onWorkStateChange,
    });

    const toggle = screen
      .getAllByRole("button", { name: /Checkout revamp/ })
      .find((button) => button.hasAttribute("aria-expanded"));
    fireEvent.click(toggle);
    fireEvent.click(toggle);

    expect(screen.getByPlaceholderText("Search…")).toHaveValue("checkout");
    expect(screen.getByLabelText("Selected job")).toHaveValue("7");
    expect(
      within(
        screen.getByText("Checkout revamp", { selector: ".work-lane-title" }).closest("section")
      ).getByText("Selected checkout")
    ).toBeInTheDocument();
    expect(onWorkStateChange).not.toHaveBeenCalled();
  });

  it("opens and closes the desktop inspector without resetting filters, grouping, or lane state", () => {
    const jobs = [makeJob({ id: 7, title: "Selected checkout", epic_id: 9, status: "running" })];
    render(
      <StatefulWorkLanes
        jobs={jobs}
        initialState={{ view: "focus", grouping: "epic", epicId: 9, search: "checkout" }}
      />
    );

    const lane = screen
      .getByText("Checkout revamp", { selector: ".work-lane-title" })
      .closest("section");
    const laneContent = lane.querySelector(".work-lane-content");
    laneContent.scrollTop = 37;
    fireEvent.click(within(lane).getByText("Selected checkout").closest("tr"));

    expect(screen.getByRole("complementary", { name: "Job #7" })).toHaveClass(
      "work-job-inspector-desktop"
    );
    expect(screen.getByPlaceholderText("Search…")).toHaveValue("checkout");
    expect(screen.getByLabelText("Group by")).toHaveValue("epic");
    expect(
      screen.getByText("Checkout revamp", { selector: ".work-lane-title" })
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Close inspector" }));
    expect(screen.queryByRole("complementary", { name: "Job #7" })).not.toBeInTheDocument();
    expect(screen.getByPlaceholderText("Search…")).toHaveValue("checkout");
    expect(screen.getByLabelText("Group by")).toHaveValue("epic");
    expect(lane.querySelector(".work-lane-content")).toBe(laneContent);
    expect(laneContent.scrollTop).toBe(37);
  });

  it("dismisses the tablet inspector from its backdrop without opening full job navigation", () => {
    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn((query) => ({
      matches: query === "(max-width: 900px)",
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
    }));
    const onOpenJob = vi.fn();
    const onWorkStateChange = vi.fn();
    const { container } = renderLanes({
      jobs: [makeJob({ id: 7, title: "Tablet checkout", status: "running" })],
      onOpenJob,
      workState: { selectedJobId: 7 },
      onWorkStateChange,
    });

    expect(screen.getByRole("dialog", { name: "Job #7" })).toBeInTheDocument();
    fireEvent.mouseDown(container.querySelector(".work-inspector-backdrop"));
    expect(onWorkStateChange).toHaveBeenCalledWith(
      expect.objectContaining({ selectedJobId: null })
    );
    expect(onOpenJob).not.toHaveBeenCalled();
    window.matchMedia = originalMatchMedia;
  });

  it("auto-expands recently finished when it is the only populated focus lane", () => {
    renderLanes({ jobs: [makeJob({ title: "Only finished", status: "done" })] });

    const toggle = screen.getByRole("button", { name: /Recently Finished/ });
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Only finished")).toBeInTheDocument();
  });

  it("shows a ready, unblocked pending job in the default view's Queued lane", () => {
    renderLanes({
      jobs: [
        makeJob({ id: 3036, title: "Acme Identity", status: "pending", waiting_on: [] }),
      ],
    });

    expect(
      within(screen.getByText("Queued").closest("section")).getByText("Acme Identity")
    ).toBeInTheDocument();
  });

  it("orders the default view's lanes per CONVENTIONS.md §9.6", () => {
    renderLanes({
      jobs: [
        makeJob({ id: 1, title: "Failed", status: "failed" }),
        makeJob({ id: 2, title: "Running", status: "running" }),
        makeJob({ id: 3, title: "Ready", status: "pending", waiting_on: [] }),
        makeJob({ id: 4, title: "Done", status: "done" }),
      ],
    });

    const titles = [...document.querySelectorAll(".work-lane-title")].map((el) => el.textContent);
    const idx = (name) => titles.indexOf(name);
    expect(idx("Needs Attention")).toBeGreaterThanOrEqual(0);
    expect(idx("Needs Attention")).toBeLessThan(idx("Running Now"));
    expect(idx("Running Now")).toBeLessThan(idx("Queued"));
    expect(idx("Queued")).toBeLessThan(idx("Recently Finished"));
  });

  it("keeps a blocked job's dependency context in Blocked, distinct from Queued", () => {
    renderLanes({
      jobs: [
        makeJob({ id: 5, title: "Ready job", status: "pending", waiting_on: [] }),
        makeJob({ id: 6, title: "Blocked job", status: "pending", waiting_on: [5] }),
      ],
    });

    const queued = screen.getByText("Queued").closest("section");
    const blocked = screen
      .getByText("Blocked", { selector: ".work-lane-title" })
      .closest("section");
    expect(within(queued).getByText("Ready job")).toBeInTheDocument();
    expect(within(queued).queryByText("Blocked job")).not.toBeInTheDocument();
    expect(within(blocked).getByText("Blocked job")).toBeInTheDocument();
    expect(within(blocked).getByText("waiting on #5")).toBeInTheDocument();
    expect(within(blocked).getByText("Complete #5")).toBeInTheDocument();
    expect(screen.getAllByText("Ready job")).toHaveLength(1);
    expect(screen.getAllByText("Blocked job")).toHaveLength(1);
  });

  it("classifies a scheduler-wait-only pending job as Blocked with its summary, distinct from dependency waits", () => {
    const schedulerWait = {
      reason: "file_overlap",
      summary:
        "Waiting for project execution slot — #3139 overlaps frontend/src/components/Layout.tsx.",
      blocking_job_ids: [3139],
      conflicting_paths: ["frontend/src/components/Layout.tsx"],
    };
    renderLanes({
      jobs: [
        makeJob({ id: 5, title: "Ready job", status: "pending", waiting_on: [] }),
        makeJob({
          id: 6,
          title: "Scheduler waiting job",
          status: "pending",
          waiting_on: [],
          scheduler_wait: schedulerWait,
        }),
      ],
    });

    const queued = screen.getByText("Queued").closest("section");
    const blocked = screen
      .getByText("Blocked", { selector: ".work-lane-title" })
      .closest("section");
    expect(within(queued).getByText("Ready job")).toBeInTheDocument();
    expect(within(queued).queryByText("Scheduler waiting job")).not.toBeInTheDocument();
    expect(within(blocked).getByText("Scheduler waiting job")).toBeInTheDocument();
    expect(within(blocked).getAllByText(schedulerWait.summary)).toHaveLength(2);
    expect(within(blocked).queryByText(/Ready to start/)).not.toBeInTheDocument();
  });

  it("still renders the dependency waiting-on message unchanged when both waiting_on and scheduler_wait are set", () => {
    renderLanes({
      jobs: [
        makeJob({
          id: 7,
          title: "Dependency waiting job",
          status: "pending",
          waiting_on: [5],
          scheduler_wait: { reason: "file_overlap", summary: "Should not render" },
        }),
      ],
    });

    const blocked = screen
      .getByText("Blocked", { selector: ".work-lane-title" })
      .closest("section");
    expect(within(blocked).getByText("waiting on #5")).toBeInTheDocument();
    expect(within(blocked).getByText("Complete #5")).toBeInTheDocument();
    expect(within(blocked).queryByText("Should not render")).not.toBeInTheDocument();
  });

  it.each(["desktop", "mobile"])(
    "classifies a scheduler-wait-only pending job as Blocked, never Queued, at %s width",
    (mode) => {
      const restore = mode === "mobile" ? mockMobileViewport() : null;
      try {
        const schedulerWait = {
          reason: "file_overlap",
          summary:
            "Waiting for project execution slot — #3139 overlaps frontend/src/components/Layout.tsx.",
          blocking_job_ids: [3139],
          conflicting_paths: ["frontend/src/components/Layout.tsx"],
        };
        renderLanes({
          jobs: [
            makeJob({ id: 5, title: "Ready job", status: "pending", waiting_on: [] }),
            makeJob({
              id: 6,
              title: "Scheduler waiting job",
              status: "pending",
              waiting_on: [],
              scheduler_wait: schedulerWait,
            }),
          ],
        });

        const queued = screen.getByText("Queued").closest("section");
        const blocked = screen
          .getByText("Blocked", { selector: ".work-lane-title" })
          .closest("section");
        expect(within(queued).getByText("Ready job")).toBeInTheDocument();
        expect(within(queued).queryByText("Scheduler waiting job")).not.toBeInTheDocument();
        expect(within(blocked).getByText("Scheduler waiting job")).toBeInTheDocument();
        expect(within(blocked).queryByText(/Ready to start/)).not.toBeInTheDocument();
      } finally {
        restore?.();
      }
    }
  );

  it.each(["desktop", "mobile"])(
    "renders a waiting_on-only pending job in Blocked unchanged at %s width",
    (mode) => {
      const restore = mode === "mobile" ? mockMobileViewport() : null;
      try {
        renderLanes({
          jobs: [
            makeJob({ id: 7, title: "Dependency waiting job", status: "pending", waiting_on: [5] }),
          ],
        });

        const blocked = screen
          .getByText("Blocked", { selector: ".work-lane-title" })
          .closest("section");
        expect(within(blocked).getByText("Dependency waiting job")).toBeInTheDocument();
        if (mode === "desktop") {
          expect(within(blocked).getByText("waiting on #5")).toBeInTheDocument();
          expect(within(blocked).getByText("Complete #5")).toBeInTheDocument();
        } else {
          expect(within(blocked).getByRole("button", { name: "#5" })).toBeInTheDocument();
        }
      } finally {
        restore?.();
      }
    }
  );

  it.each(["desktop", "mobile"])(
    "keeps a pending job with neither waiting_on nor scheduler_wait in Queued at %s width",
    (mode) => {
      const restore = mode === "mobile" ? mockMobileViewport() : null;
      try {
        renderLanes({
          jobs: [
            makeJob({ id: 3036, title: "Acme Identity", status: "pending", waiting_on: [] }),
          ],
        });

        expect(
          within(screen.getByText("Queued").closest("section")).getByText("Acme Identity")
        ).toBeInTheDocument();
        const blocked = screen
          .getByText("Blocked", { selector: ".work-lane-title" })
          .closest("section");
        expect(within(blocked).queryByText("Acme Identity")).not.toBeInTheDocument();
      } finally {
        restore?.();
      }
    }
  );
});
