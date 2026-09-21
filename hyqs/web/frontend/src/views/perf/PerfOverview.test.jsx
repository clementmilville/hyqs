import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { PerfOverview } from "./PerfOverview.jsx";
import { getPerfStageStats } from "../../api.js";

vi.mock("../../api.js", () => ({
  getPerfHeadline: vi.fn(() =>
    Promise.resolve({
      total_jobs: 1,
      success_rate: 1,
      avg_cycle_time_s: 10,
      total_tokens: 100,
      total_cost_usd: 1,
    })
  ),
  getPerfStageStats: vi.fn(() => Promise.resolve([])),
  getPerfSlowestJobs: vi.fn(() =>
    Promise.resolve([
      {
        job_id: 42,
        idea: "A".repeat(400),
        title: "Slow job title",
        stage: "build",
        status: "done",
        total_duration_s: 125,
      },
    ])
  ),
}));

describe("PerfOverview Slowest Jobs list", () => {
  it("renders one summary line per job, with the full idea hidden behind an expander", async () => {
    render(<PerfOverview projectId={1} filters={{}} />);

    expect(await screen.findByText("#42")).toBeInTheDocument();
    expect(screen.getByText("Slow job title")).toBeInTheDocument();
    // The full idea text is collapsed behind an expander, not open by default...
    const details = document.querySelector(".perf-slowest-idea-details");
    expect(details).toBeInTheDocument();
    expect(details.open).toBe(false);
    expect(details.querySelector("summary")).toHaveTextContent("idea");
    // ...but is reachable via the expander's content.
    expect(details.querySelector("pre").textContent).toBe("A".repeat(400));
  });
});

describe("PerfOverview Stage Duration chart", () => {
  it("renders Avg/P50/P95 legend labels for non-empty stage data", async () => {
    getPerfStageStats.mockResolvedValueOnce([
      {
        stage: "build",
        run_count: 5,
        avg_duration_s: 300,
        p50_duration_s: 300,
        p95_duration_s: 480,
      },
    ]);

    render(<PerfOverview projectId={1} filters={{}} />);

    expect(await screen.findByText("Avg (s)")).toBeInTheDocument();
    expect(screen.getByText("P50 (s)")).toBeInTheDocument();
    expect(screen.getByText("P95 (s)")).toBeInTheDocument();
  });
});
