import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { AdminUsageCost } from "./AdminUsageCost.jsx";

vi.mock("../api.js", () => ({
  getUsage: vi.fn(),
  getJobsFiledByBreakdown: vi.fn(),
  getDeploymentReliability: vi.fn(),
}));

const BASE_USAGE = {
  total_tokens: 1000,
  total_cost_usd: 10,
  by_source: [],
  projects: [],
};

const BASE_BREAKDOWN = [
  { source: "ui", source_actor: "alice@example.com", count: 5 },
  { source: "ui", source_actor: "bob@example.com", count: 2 },
  { source: "slack", source_actor: "#eng-alerts", count: 3 },
];

const BASE_RELIABILITY = [
  {
    project_id: 1,
    project_name: "Payments",
    total: 20,
    done: 15,
    failed: 3,
    cancelled: 2,
    failure_rate: 0.15,
  },
];

describe("AdminUsageCost", () => {
  beforeEach(async () => {
    const api = await import("../api.js");
    vi.clearAllMocks();
    api.getDeploymentReliability.mockResolvedValue(BASE_RELIABILITY);
  });

  it("renders the reused UsageView cross-project rollup", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    expect(await screen.findByText("By project → epic → job")).toBeInTheDocument();
  });

  it("shows the access-denied state when cross-project usage is forbidden", async () => {
    const api = await import("../api.js");
    api.getUsage.mockRejectedValue(new Error("forbidden"));
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
    expect(screen.queryByText("By project → epic → job")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Loading")).not.toBeInTheDocument();
  });

  it("groups filed-by data by source with an expandable per-actor breakdown", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    expect(await screen.findByText("ui", { selector: ".u-stage-source" })).toBeInTheDocument();
    expect(screen.getByText("slack", { selector: ".u-stage-source" })).toBeInTheDocument();
    expect(screen.getByText("7 jobs")).toBeInTheDocument();
    expect(screen.queryByText("alice@example.com")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("ui", { selector: ".u-stage-source" }));

    expect(await screen.findByText("alice@example.com")).toBeInTheDocument();
    expect(screen.getByText("bob@example.com")).toBeInTheDocument();
  });

  it("shows the access-denied message when getJobsFiledByBreakdown is forbidden", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockRejectedValue(new Error("forbidden"));

    render(<AdminUsageCost />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
  });

  it("shows an empty-state hint when the breakdown is empty", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue([]);

    render(<AdminUsageCost />);

    expect(await screen.findByText("No jobs filed yet.")).toBeInTheDocument();
  });

  it("shows a working Retry on a generic fetch failure", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockRejectedValueOnce(new Error("network down"));
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);

    await waitFor(() => expect(screen.getByText("ui")).toBeInTheDocument());
  });

  it("renders deployment reliability metrics by project", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    expect(await screen.findByText("Deployment reliability")).toBeInTheDocument();
    const row = await screen.findByRole("row", { name: /Payments/ });
    expect(
      within(row)
        .getAllByRole("cell")
        .map((cell) => cell.textContent)
    ).toEqual(["Payments", "20", "15", "3", "2", "15.0%"]);
    expect(api.getDeploymentReliability).toHaveBeenCalledWith(undefined);
  });

  it("shows an empty state when deployment reliability has no projects", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);
    api.getDeploymentReliability.mockResolvedValue([]);

    render(<AdminUsageCost />);

    expect(await screen.findByText("No deployment reliability data yet.")).toBeInTheDocument();
  });

  it("links to the visitor analytics page where the page-views section used to be", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);
    api.getJobsFiledByBreakdown.mockResolvedValue(BASE_BREAKDOWN);

    render(<AdminUsageCost />);

    const link = await screen.findByRole("link", { name: /visitor analytics/i });
    expect(link).toHaveAttribute("href", "#admin/visitor-analytics");
  });
});
