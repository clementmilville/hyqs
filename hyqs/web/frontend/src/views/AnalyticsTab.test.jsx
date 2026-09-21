import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { AnalyticsTab } from "./AnalyticsTab.jsx";
import { getUsage, getPerfHeadline, getPerfTrend } from "../api.js";

vi.mock("../api.js", () => ({
  getUsage: vi.fn(),
  getPerfHeadline: vi.fn(),
  getPerfTrend: vi.fn(),
}));

vi.mock("./UsageView.jsx", () => ({
  UsageView: () => <div data-testid="usage-view" />,
}));
vi.mock("./PerformanceTab.jsx", () => ({
  PerformanceTab: () => <div data-testid="performance-tab" />,
}));

const project = { id: 1, repo_path: "/repo" };

const usage = { total_cost_usd: 12.5, total_tokens: 1000, projects: [], by_source: [] };
const headline = {
  total_jobs: 8,
  success_rate: 0.75,
  avg_cycle_time_s: 125,
  excludes_operational: true,
  operational_jobs_excluded: 2,
};
const trend = [
  {
    date: "2026-07-20",
    cost_usd: 1.5,
    tokens: 100,
    jobs_completed: 2,
    jobs_failed: 0,
    success_rate: 1,
  },
  {
    date: "2026-07-21",
    cost_usd: 2.5,
    tokens: 200,
    jobs_completed: 1,
    jobs_failed: 1,
    success_rate: 0.5,
  },
];

function mockApiSuccess() {
  getUsage.mockResolvedValue(usage);
  getPerfHeadline.mockResolvedValue(headline);
  getPerfTrend.mockResolvedValue(trend);
}

describe("AnalyticsTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders a stat row from the mocked usage/headline data with no sub-tab switcher", async () => {
    mockApiSuccess();
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(screen.getByText("$12.50")).toBeInTheDocument());
    expect(screen.getByText("8")).toBeInTheDocument();
    expect(screen.getByText("75.0%")).toBeInTheDocument();
    expect(screen.getByText("2m 5s")).toBeInTheDocument();
    expect(screen.queryByText("Cost")).not.toBeInTheDocument();
    expect(screen.queryByText("Pipeline")).not.toBeInTheDocument();
  });

  it("shows a caption stating how many auto-deploy jobs were excluded", async () => {
    mockApiSuccess();
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(screen.getByText(/exclude 2 auto-deploy jobs/)).toBeInTheDocument());
  });

  it("omits the exclusion caption when the headline lacks exclusion metadata", async () => {
    getUsage.mockResolvedValue(usage);
    getPerfHeadline.mockResolvedValue({
      total_jobs: 8,
      success_rate: 0.75,
      avg_cycle_time_s: 125,
    });
    getPerfTrend.mockResolvedValue(trend);
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(screen.getByText("8")).toBeInTheDocument());
    expect(screen.queryByText(/auto-deploy job/)).not.toBeInTheDocument();
  });

  it("renders both trend charts", async () => {
    mockApiSuccess();
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(screen.getByText("Cost Over Time")).toBeInTheDocument());
    expect(screen.getByText("Throughput & Success")).toBeInTheDocument();
  });

  it("reveals the Cost breakdown and Pipeline detail sections when expanded", async () => {
    mockApiSuccess();
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(screen.getByText("Cost breakdown")).toBeInTheDocument());
    expect(screen.queryByTestId("usage-view")).toBeInTheDocument();
    expect(screen.queryByTestId("performance-tab")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Cost breakdown"));
    fireEvent.click(screen.getByText("Pipeline detail"));
    expect(screen.getByTestId("usage-view")).toBeInTheDocument();
    expect(screen.getByTestId("performance-tab")).toBeInTheDocument();
  });

  it("refetches when the period picker changes", async () => {
    mockApiSuccess();
    render(<AnalyticsTab project={project} />);

    await waitFor(() => expect(getUsage).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByText("Week"));
    await waitFor(() => expect(getUsage).toHaveBeenCalledTimes(2));
    expect(getPerfHeadline).toHaveBeenCalledTimes(2);
    expect(getPerfTrend).toHaveBeenCalledTimes(2);
  });

  it("shows the forbidden message when the API rejects with forbidden", async () => {
    getUsage.mockRejectedValue(new Error("forbidden"));
    getPerfHeadline.mockResolvedValue(headline);
    getPerfTrend.mockResolvedValue(trend);
    render(<AnalyticsTab project={project} />);

    await waitFor(() =>
      expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument()
    );
  });

  it("shows an error + retry button when the API fails for a non-forbidden reason", async () => {
    getUsage.mockRejectedValue(new Error("boom"));
    getPerfHeadline.mockResolvedValue(headline);
    getPerfTrend.mockResolvedValue(trend);
    render(<AnalyticsTab project={project} />);

    await waitFor(() =>
      expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument()
    );
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("shows a loading spinner before data arrives", () => {
    getUsage.mockReturnValue(new Promise(() => {}));
    getPerfHeadline.mockReturnValue(new Promise(() => {}));
    getPerfTrend.mockReturnValue(new Promise(() => {}));
    render(<AnalyticsTab project={project} />);

    expect(screen.queryByText("Cost Over Time")).not.toBeInTheDocument();
  });
});
