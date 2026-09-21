import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { AdminCommandCenter } from "./AdminCommandCenter.jsx";
import {
  streamWorkers,
  getWorkers,
  streamSupervisor,
  getSupervisor,
  getProjectsRuntime,
} from "../api.js";

vi.mock("../api.js", () => ({
  streamWorkers: vi.fn(),
  getWorkers: vi.fn(),
  streamSupervisor: vi.fn(),
  getSupervisor: vi.fn(),
  getProjectsRuntime: vi.fn(),
}));

function fakeStream() {
  return { close: vi.fn() };
}

beforeEach(() => {
  vi.clearAllMocks();
  streamWorkers.mockReturnValue(fakeStream());
  streamSupervisor.mockReturnValue(fakeStream());
  getProjectsRuntime.mockResolvedValue({ projects: [] });
});

describe("AdminCommandCenter section order", () => {
  it("renders all 4 lane titles in order: Needs Attention, Running Now, Queued, Recently Finished", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [],
    });

    render(<AdminCommandCenter />);

    const titles = await screen.findAllByText(
      /^(Needs Attention|Running Now|Queued|Recently Finished)$/
    );
    expect(titles.map((t) => t.textContent)).toEqual([
      "Needs Attention",
      "Running Now",
      "Queued",
      "Recently Finished",
    ]);
  });
});

describe("AdminCommandCenter remediation feed", () => {
  it("shows a one-line summary from parsed diagnosis JSON, full payload behind an expander", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [
        {
          id: 1,
          ts: Date.now() / 1000,
          job_id: 9,
          action: "requeued",
          failure_class: "transient",
          detail: JSON.stringify({
            diagnosis: "First line of diagnosis.\nMore detail here.",
            action: { type: "requeue" },
            confidence: 0.8,
            valid: true,
          }),
        },
      ],
    });

    render(<AdminCommandCenter />);

    expect(await screen.findByText("First line of diagnosis.")).toBeInTheDocument();
    const details = document.querySelector(".sup-detail details");
    expect(details.open).toBe(false);
    expect(details.querySelector(".sup-detail-full").textContent).toContain("More detail here.");
  });

  it("falls back to the raw string for non-JSON detail", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [
        {
          id: 2,
          ts: Date.now() / 1000,
          job_id: 3,
          action: "escalated",
          failure_class: "genuine_code",
          detail: "plain text detail",
        },
      ],
    });

    render(<AdminCommandCenter />);

    expect(await screen.findByText("plain text detail")).toBeInTheDocument();
  });
});

describe("AdminCommandCenter remediation feed coalescing + windowing", () => {
  it("collapses consecutive events sharing job_id+failure_class into one row with a xN chip", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    const now = Date.now() / 1000;
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [
        { id: 3, ts: now, job_id: 9, action: "requeued", failure_class: "transient", detail: "" },
        {
          id: 2,
          ts: now - 60,
          job_id: 9,
          action: "requeued",
          failure_class: "transient",
          detail: "",
        },
        {
          id: 1,
          ts: now - 120,
          job_id: 9,
          action: "requeued",
          failure_class: "transient",
          detail: "",
        },
      ],
    });

    render(<AdminCommandCenter />);

    expect(await screen.findByText("×3")).toBeInTheDocument();
    expect(screen.getAllByText("#9")).toHaveLength(1);
  });

  it("hides events older than 24h by default, revealing them via Show earlier", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    const now = Date.now() / 1000;
    const recentTs = now - 60;
    const oldTs = now - 25 * 60 * 60;
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [
        {
          id: 2,
          ts: recentTs,
          job_id: 10,
          action: "requeued",
          failure_class: "transient",
          detail: "",
        },
        {
          id: 1,
          ts: oldTs,
          job_id: 11,
          action: "escalated",
          failure_class: "genuine_code",
          detail: "",
        },
      ],
    });

    render(<AdminCommandCenter />);

    expect(await screen.findByText("#10")).toBeInTheDocument();
    expect(screen.queryByText("#11")).not.toBeInTheDocument();

    const showEarlierBtn = screen.getByText(/Show earlier \(1\)/);
    fireEvent.click(showEarlierBtn);

    expect(await screen.findByText("#11")).toBeInTheDocument();
  });
});

describe("AdminCommandCenter resilient fetching", () => {
  it("shows a Retry button instead of an eternal spinner on connection failure", async () => {
    getWorkers.mockRejectedValue(new Error("network down"));
    getSupervisor.mockRejectedValue(new Error("network down"));

    render(<AdminCommandCenter />);

    const retryBtn = await screen.findByText("Retry");
    expect(retryBtn).toBeInTheDocument();

    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: { alive: 1 } });
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [],
    });
    fireEvent.click(retryBtn);

    expect(await screen.findByText("workers online")).toBeInTheDocument();
  });

  it("shows a clear no-access message on a forbidden error", async () => {
    getWorkers.mockRejectedValue(new Error("forbidden"));
    getSupervisor.mockRejectedValue(new Error("forbidden"));

    render(<AdminCommandCenter />);

    expect(await screen.findByText(/don.t have access/i)).toBeInTheDocument();
  });
});

describe("AdminCommandCenter sub-tabs", () => {
  beforeEach(() => {
    getWorkers.mockResolvedValue({
      workers: [],
      pauses: [],
      merge_locks: [{ repo_path: "/home/hyqs/projects/demo", owner: "job-42" }],
      stats: {},
    });
    getSupervisor.mockResolvedValue({
      supervisors: [],
      needs_judgment: [],
      dead_lettered: [],
      remediation_feed: [],
    });
  });

  it("renders the job-tab-bar with Pipeline, Deployments, Runtime", async () => {
    render(<AdminCommandCenter subTab="pipeline" onNavTab={() => {}} />);
    expect(await screen.findByText("Pipeline")).toBeInTheDocument();
    expect(screen.getByText("Deployments")).toBeInTheDocument();
    expect(screen.getByText("Runtime")).toBeInTheDocument();
  });

  it("defaults to the Pipeline tab and renders the attention lanes without a merge_locks banner", async () => {
    render(<AdminCommandCenter />);
    expect(await screen.findByText("Needs Attention")).toBeInTheDocument();
    expect(screen.queryByText(/Merging:/)).not.toBeInTheDocument();
  });

  it("Deployments tab lists active merge locks", async () => {
    render(<AdminCommandCenter subTab="deployments" onNavTab={() => {}} />);
    expect(await screen.findByText("demo")).toBeInTheDocument();
    expect(screen.getByText("job-42")).toBeInTheDocument();
  });

  it("Deployments tab shows an empty state when no merges are active", async () => {
    getWorkers.mockResolvedValue({ workers: [], pauses: [], merge_locks: [], stats: {} });
    render(<AdminCommandCenter subTab="deployments" onNavTab={() => {}} />);
    expect(await screen.findByText("No active merges")).toBeInTheDocument();
  });

  it("Runtime tab calls getProjectsRuntime once on activation and shows a drift warning badge", async () => {
    // Lighter parity check; exhaustive AdminRuntime coverage (detail panel,
    // caching, empty/forbidden/error states) lives in AdminRuntime.test.jsx.
    getProjectsRuntime.mockResolvedValue({
      projects: [
        {
          project_id: 1,
          name: "demo",
          deploy_mode: "single",
          overall: "running",
          health_ok: true,
          drift_mismatch: true,
        },
      ],
    });

    render(<AdminCommandCenter subTab="runtime" onNavTab={() => {}} />);

    expect(await screen.findByText("⚠ drift")).toBeInTheDocument();
    expect(getProjectsRuntime).toHaveBeenCalledTimes(1);
  });

  it("clicking a sub-tab button calls onNavTab with the tab id", async () => {
    const onNavTab = vi.fn();
    render(<AdminCommandCenter subTab="pipeline" onNavTab={onNavTab} />);
    fireEvent.click(await screen.findByText("Runtime"));
    expect(onNavTab).toHaveBeenCalledWith("runtime");
  });
});
