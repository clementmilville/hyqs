import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import { act, render, screen, fireEvent, within } from "@testing-library/react";
import { getJobDiff, getJobEvents } from "../api.js";
import { defaultTab, JobDetailTabs } from "./JobDetailTabs.jsx";
import { JobLiveTab } from "./JobLiveTab.jsx";
import { JobOverviewTab } from "./JobOverviewTab.jsx";
import { JobTimeline } from "./JobTimeline.jsx";

vi.mock("../api.js", () => ({
  getJobEvents: vi.fn(() => Promise.resolve([])),
  getJobDiff: vi.fn(() =>
    Promise.resolve({ base: "main", branch: "feat", diff: "", truncated: false })
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

const JOB_BASE = {
  id: 1,
  idea: "test",
  status: "done",
  stage: null,
  started_at: null,
  ended_at: null,
  error: null,
  archived: false,
};

const PROJECT_BASE = { id: 1, max_fix_attempts: null };

describe("defaultTab", () => {
  it.each(["running", "pending", "deploying"])("returns 'live' for %s jobs", (status) => {
    expect(defaultTab({ ...JOB_BASE, status })).toBe("live");
  });

  it("returns 'resolution' for failed jobs", () => {
    expect(defaultTab({ ...JOB_BASE, status: "failed" })).toBe("resolution");
  });

  it.each(["done", "cancelled"])("returns 'overview' for %s jobs", (status) => {
    expect(defaultTab({ ...JOB_BASE, status })).toBe("overview");
  });
});

describe("JobDetailTabs tab bar", () => {
  it("renders all four tab buttons", async () => {
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} />);
    expect(screen.getByText("OVERVIEW")).toBeInTheDocument();
    expect(screen.getByText("LIVE")).toBeInTheDocument();
    expect(screen.getByText("STAGES")).toBeInTheDocument();
    expect(screen.getByText("DIFF")).toBeInTheDocument();
  });

  it("clicking a tab button switches active tab", async () => {
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} />);
    const liveBtn = screen.getByText("LIVE");
    fireEvent.click(liveBtn);
    expect(liveBtn.className).toMatch(/active/);
    expect(screen.getByText("OVERVIEW").className).not.toMatch(/active/);
  });

  it("renders the DEPENDS tab button and clicking it switches active tab", async () => {
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} />);
    const dependsBtn = screen.getByText("DEPENDS");
    expect(dependsBtn).toBeInTheDocument();
    fireEvent.click(dependsBtn);
    expect(dependsBtn.className).toMatch(/active/);
    expect(screen.getByText("OVERVIEW").className).not.toMatch(/active/);
  });

  it("renders the COST tab button and clicking it switches active tab and shows JobCostTab content", async () => {
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} />);
    const costBtn = screen.getByText("COST");
    expect(costBtn).toBeInTheDocument();
    fireEvent.click(costBtn);
    expect(costBtn.className).toMatch(/active/);
    expect(screen.getByText("OVERVIEW").className).not.toMatch(/active/);
    expect(await screen.findByText("No usage recorded yet.")).toBeInTheDocument();
  });

  it.each(["done", "cancelled"])("%s job opens on overview by default", (status) => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status }} project={PROJECT_BASE} />);
    expect(screen.getByText("OVERVIEW")).toHaveClass("active");
  });

  it.each(["running", "pending", "deploying"])("%s job opens on live by default", (status) => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status }} project={PROJECT_BASE} />);
    expect(screen.getByText("LIVE")).toHaveClass("active");
  });

  it("failed job opens on resolution by default", () => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status: "failed" }} project={PROJECT_BASE} />);
    expect(screen.getByText("RESOLUTION").className).toMatch(/active/);
  });

  it("starts with no mounted panel in inspector mode and mounts only the selected panel", async () => {
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} events={[]} inspectorMode />);

    expect(screen.getByText("OVERVIEW")).not.toHaveClass("active");
    expect(screen.queryByText("No usage recorded yet.")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("COST"));
    expect(await screen.findByText("No usage recorded yet.")).toBeInTheDocument();

    fireEvent.click(screen.getByText("LIVE"));
    expect(screen.queryByText("No usage recorded yet.")).not.toBeInTheDocument();
    expect(screen.getByText(/not currently running/i)).toBeInTheDocument();
  });

  it("reuses supplied events without requesting them again", () => {
    getJobEvents.mockClear();
    const events = [{ id: 4, stage: "build", status: "done", detail: {} }];
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} events={events} inspectorMode />);
    expect(getJobEvents).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("STAGES"));
    expect(
      screen.getByText((_, element) => element.classList.contains("tl-stage"))
    ).toHaveTextContent("Build");
    expect(getJobEvents).not.toHaveBeenCalled();
  });

  it("does not start a heavy tab request until that tab is selected", async () => {
    getJobDiff.mockClear();
    render(<JobDetailTabs job={JOB_BASE} project={PROJECT_BASE} events={[]} inspectorMode />);
    expect(getJobDiff).not.toHaveBeenCalled();
    fireEvent.click(screen.getByText("DIFF"));
    expect(getJobDiff).toHaveBeenCalledWith(JOB_BASE.id);
  });
});

describe("JobDetailTabs event polling", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    getJobEvents.mockClear();
  });

  afterEach(() => {
    vi.runOnlyPendingTimers();
    vi.useRealTimers();
  });

  it.each(["running", "pending", "deploying"])(
    "continues polling events for %s jobs",
    async (status) => {
      const { unmount } = render(
        <JobDetailTabs job={{ ...JOB_BASE, status }} project={PROJECT_BASE} />
      );
      expect(getJobEvents).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(6000);
      });

      expect(getJobEvents).toHaveBeenCalledTimes(3);
      unmount();
    }
  );

  it.each(["done", "failed", "cancelled"])(
    "loads events once without continued polling for %s jobs",
    async (status) => {
      render(<JobDetailTabs job={{ ...JOB_BASE, status }} project={PROJECT_BASE} />);
      expect(getJobEvents).toHaveBeenCalledTimes(1);

      await act(async () => {
        vi.advanceTimersByTime(6000);
      });

      expect(getJobEvents).toHaveBeenCalledTimes(1);
    }
  );

  it("stops polling when a deploying job transitions to done", async () => {
    const { rerender } = render(
      <JobDetailTabs job={{ ...JOB_BASE, status: "deploying" }} project={PROJECT_BASE} />
    );
    expect(screen.getByText("LIVE")).toHaveClass("active");
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for log output");
    expect(getJobEvents).toHaveBeenCalledTimes(1);

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });
    expect(getJobEvents).toHaveBeenCalledTimes(2);

    rerender(<JobDetailTabs job={{ ...JOB_BASE, status: "done" }} project={PROJECT_BASE} />);
    expect(getJobEvents).toHaveBeenCalledTimes(3);
    await act(async () => {
      vi.advanceTimersByTime(6000);
    });
    expect(getJobEvents).toHaveBeenCalledTimes(3);
  });
});

describe("JobDetailTabs RESOLUTION tab", () => {
  it("is absent for a non-failed job", () => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status: "done" }} project={PROJECT_BASE} />);
    expect(screen.queryByText("RESOLUTION")).not.toBeInTheDocument();
  });

  it("is present and active-by-default for a failed job", () => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status: "failed" }} project={PROJECT_BASE} />);
    const btn = screen.getByText("RESOLUTION");
    expect(btn).toBeInTheDocument();
    expect(btn.className).toMatch(/active/);
  });

  it("renders JobResolutionPanel and FixBudgetPanel content when active", () => {
    render(<JobDetailTabs job={{ ...JOB_BASE, status: "failed" }} project={PROJECT_BASE} />);
    expect(screen.getByText("Why it failed")).toBeInTheDocument();
    expect(screen.getByText("🔁 Fix budget")).toBeInTheDocument();
  });
});

describe("JobLiveTab", () => {
  it("shows idle message when job is done", () => {
    render(<JobLiveTab job={{ ...JOB_BASE, status: "done" }} />);
    expect(screen.getByText(/not currently running/i)).toBeInTheDocument();
  });

  it("shows idle message when job is failed", () => {
    render(<JobLiveTab job={{ ...JOB_BASE, status: "failed" }} />);
    expect(screen.getByText(/not currently running/i)).toBeInTheDocument();
  });

  it("shows live log state when job is pending", () => {
    render(<JobLiveTab job={{ ...JOB_BASE, status: "pending" }} />);
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for log output");
  });

  it("shows status badge in idle state", () => {
    render(<JobLiveTab job={{ ...JOB_BASE, status: "cancelled" }} />);
    expect(screen.getByText("cancelled")).toBeInTheDocument();
  });
});

describe("JobOverviewTab", () => {
  const EVENTS = [
    {
      id: 1,
      status: "done",
      stage: "plan",
      attempt: 0,
      tokens: 100,
      cost_usd: 0.001,
      started_at: "2026-01-01T00:00:00Z",
      ended_at: "2026-01-01T00:01:00Z",
      summary: "Did plan",
    },
    {
      id: 2,
      status: "done",
      stage: "build",
      attempt: 1,
      tokens: 200,
      cost_usd: 0.002,
      started_at: "2026-01-01T00:01:00Z",
      ended_at: "2026-01-01T00:03:00Z",
      summary: "Built it",
    },
  ];

  it("renders without crashing when all fields are null", () => {
    const job = { ...JOB_BASE, status: "done", started_at: null, ended_at: null, error: null };
    const { container } = render(<JobOverviewTab job={job} events={[]} />);
    expect(container).toBeTruthy();
  });

  it("shows total token sum across events", () => {
    render(<JobOverviewTab job={JOB_BASE} events={EVENTS} />);
    expect(screen.getByText("300")).toBeInTheDocument();
  });

  it("shows total cost across events", () => {
    render(<JobOverviewTab job={JOB_BASE} events={EVENTS} />);
    expect(screen.getByText("$0.0030")).toBeInTheDocument();
  });

  it("counts stages correctly", () => {
    render(<JobOverviewTab job={JOB_BASE} events={EVENTS} />);
    expect(screen.getByText("2")).toBeInTheDocument();
  });

  it("counts retries (attempt > 0)", () => {
    render(<JobOverviewTab job={JOB_BASE} events={EVENTS} />);
    expect(screen.getByText("1")).toBeInTheDocument();
  });

  it("shows job error when present", () => {
    const job = { ...JOB_BASE, error: "Something went wrong" };
    render(<JobOverviewTab job={job} events={[]} />);
    expect(screen.getByText("Something went wrong")).toBeInTheDocument();
  });

  it("falls back to last event summary when no error", () => {
    render(<JobOverviewTab job={JOB_BASE} events={EVENTS} />);
    expect(screen.getByText("Built it")).toBeInTheDocument();
  });

  it("shows dashes when no token/cost data", () => {
    render(<JobOverviewTab job={JOB_BASE} events={[]} />);
    const dashes = screen.getAllByText("—");
    expect(dashes.length).toBeGreaterThanOrEqual(2);
  });
});

describe("JobTimeline", () => {
  // Use stages that render ev.summary in EventDetail (review/build, not plan)
  const EVENTS = [
    {
      id: 10,
      status: "done",
      stage: "review",
      attempt: 0,
      tokens: 50,
      cost_usd: 0.0005,
      started_at: "2026-01-01T00:00:00Z",
      ended_at: "2026-01-01T00:01:00Z",
      summary: "Review summary",
      detail: {},
    },
    {
      id: 11,
      status: "failed",
      stage: "build",
      attempt: 0,
      tokens: 80,
      cost_usd: 0.001,
      started_at: "2026-01-01T00:01:00Z",
      ended_at: "2026-01-01T00:02:00Z",
      summary: "Build failed",
      detail: {},
    },
  ];

  it("renders all rows collapsed by default", () => {
    render(<JobTimeline job={JOB_BASE} events={EVENTS} />);
    expect(screen.queryByText("Plan summary")).not.toBeInTheDocument();
    expect(screen.queryByText("Build failed")).not.toBeInTheDocument();
  });

  it("clicking a row header expands its EventDetail", () => {
    render(<JobTimeline job={JOB_BASE} events={EVENTS} />);
    const heads = document.querySelectorAll(".tl-head");
    fireEvent.click(heads[0]);
    expect(screen.getByText("Review summary")).toBeInTheDocument();
  });

  it("clicking an expanded row collapses it", () => {
    render(<JobTimeline job={JOB_BASE} events={EVENTS} />);
    const heads = document.querySelectorAll(".tl-head");
    fireEvent.click(heads[0]);
    expect(screen.getByText("Review summary")).toBeInTheDocument();
    fireEvent.click(heads[0]);
    expect(screen.queryByText("Review summary")).not.toBeInTheDocument();
  });

  it("autoExpandFailed opens last failed row on mount", () => {
    render(<JobTimeline job={JOB_BASE} events={EVENTS} autoExpandFailed={true} />);
    expect(screen.getByText("Build failed")).toBeInTheDocument();
    expect(screen.queryByText("Review summary")).not.toBeInTheDocument();
  });

  it("attributes mixed-agent rows while preserving metrics and interactions", () => {
    const enrichedEvents = [
      {
        ...EVENTS[0],
        agent_name: "Ada Reviewer",
        agent_provider: "anthropic",
        agent_model: "claude-sonnet",
      },
      {
        ...EVENTS[1],
        agent_name: "Casey Coder",
        agent_provider: "openai",
        agent_model: "gpt-5",
      },
    ];

    render(<JobTimeline job={JOB_BASE} events={enrichedEvents} />);
    const heads = document.querySelectorAll(".tl-head");

    expect(
      within(heads[0]).getByText("Ada Reviewer · anthropic · claude-sonnet")
    ).toBeInTheDocument();
    expect(within(heads[1]).getByText("Casey Coder · openai · gpt-5")).toBeInTheDocument();
    expect(within(heads[0]).getByText(/1m/)).toBeInTheDocument();
    expect(within(heads[0]).getByText(/50 tok/)).toBeInTheDocument();
    expect(within(heads[0]).getByText(/\$0\.0005/)).toBeInTheDocument();
    expect(document.querySelectorAll(".tl-row.bad")).toHaveLength(1);

    fireEvent.click(heads[0]);
    expect(screen.getByText("Review summary")).toBeInTheDocument();
    fireEvent.click(heads[0]);
    expect(screen.queryByText("Review summary")).not.toBeInTheDocument();
  });

  it("omits attribution for deterministic and legacy unassigned rows", () => {
    const unassignedEvents = [
      { ...EVENTS[0], agent_name: null, agent_provider: null, agent_model: null },
      { ...EVENTS[1], agent_id: 42 },
    ];

    render(<JobTimeline job={JOB_BASE} events={unassignedEvents} />);

    expect(document.querySelectorAll(".tl-head .hint")).toHaveLength(0);
    expect(screen.queryByText(/unknown|unassigned|null|undefined/i)).not.toBeInTheDocument();
  });

  it("renders partial executor metadata without empty separators", () => {
    render(
      <JobTimeline
        job={JOB_BASE}
        events={[
          {
            ...EVENTS[0],
            agent_name: "Riley",
            agent_provider: "",
            agent_model: "gpt-5",
          },
        ]}
      />
    );

    expect(screen.getByText("Riley · gpt-5")).toBeInTheDocument();
    expect(screen.queryByText(/Riley · ·/)).not.toBeInTheDocument();
  });

  it("shows empty hint when events array is empty", () => {
    render(<JobTimeline job={JOB_BASE} events={[]} />);
    expect(screen.getByText(/no step activity/i)).toBeInTheDocument();
  });
});
