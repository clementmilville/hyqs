import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PerformanceTab } from "./PerformanceTab.jsx";

vi.mock("../api.js", () => ({
  getPerfHeadline: vi.fn(),
  getPerfStageStats: vi.fn(() => Promise.resolve([])),
  getPerfSlowestJobs: vi.fn(() => Promise.resolve([])),
  listEpics: vi.fn(() => Promise.resolve([])),
}));

const HEADLINE = {
  total_jobs: 3,
  success_rate: 1,
  avg_cycle_time_s: 10,
  total_tokens: 100,
  total_cost_usd: 1,
};

describe("PerformanceTab three-state rendering", () => {
  it("shows a Spinner while the initial fetch is in flight", async () => {
    const api = await import("../api.js");
    api.getPerfHeadline.mockReturnValue(new Promise(() => {}));

    render(<PerformanceTab projectId={1} />);

    expect(document.querySelector(".spinner")).toBeInTheDocument();
  });

  it("shows the access-denied message when the perf headline fetch is forbidden", async () => {
    const api = await import("../api.js");
    api.getPerfHeadline.mockRejectedValue(new Error("forbidden"));

    render(<PerformanceTab projectId={1} />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
  });

  it("shows an error message with a working Retry button on a generic fetch failure", async () => {
    const api = await import("../api.js");
    api.getPerfHeadline.mockRejectedValueOnce(new Error("network down"));
    api.getPerfHeadline.mockResolvedValue(HEADLINE);

    render(<PerformanceTab projectId={1} />);

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);

    await waitFor(() => expect(screen.getByText("Total Jobs")).toBeInTheDocument());
  });

  it("renders overview data once all three fetches resolve", async () => {
    const api = await import("../api.js");
    api.getPerfHeadline.mockResolvedValue(HEADLINE);

    render(<PerformanceTab projectId={1} />);

    expect(await screen.findByText("Total Jobs")).toBeInTheDocument();
    expect(
      screen.getByText("Pipeline metrics exclude operational auto-deploy jobs.")
    ).toBeInTheDocument();
  });
});
