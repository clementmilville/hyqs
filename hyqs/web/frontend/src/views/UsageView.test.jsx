import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { UsageView } from "./UsageView.jsx";

vi.mock("../api.js", () => ({
  getUsage: vi.fn(),
}));

const BASE_USAGE = {
  total_tokens: 1000,
  total_cost_usd: 10,
  by_source: [],
  projects: [
    {
      project_id: null,
      name: "Interactive dev sessions (Claude Code)",
      tokens: 620,
      cost_usd: 6.2,
      runs: 4,
      epics: [],
    },
    {
      project_id: 1,
      name: "Demo project",
      tokens: 380,
      cost_usd: 3.8,
      runs: 2,
      epics: [
        {
          epic_id: 5,
          name: "Checkout",
          tokens: 380,
          cost_usd: 3.8,
          runs: 2,
          jobs: [
            { job_id: 42, idea: "do the thing", title: "", tokens: 380, cost_usd: 3.8, runs: 2 },
          ],
        },
      ],
    },
  ],
};

describe("UsageView dev-session bucket", () => {
  it("labels the pid-None bucket and renders it as a flat, non-expandable row", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView />);

    expect(await screen.findByText(/Interactive dev sessions \(Claude Code\)/)).toBeInTheDocument();
    expect(screen.queryByText("Non-pipeline")).not.toBeInTheDocument();
    expect(screen.queryByText("Unassigned")).not.toBeInTheDocument();
  });

  it("still renders an expandable row for a project with epics", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView />);

    expect(await screen.findByText(/Demo project/)).toBeInTheDocument();
    expect(screen.getByText(/Checkout/)).toBeInTheDocument();
  });
});

describe("UsageView projectId scoping", () => {
  it("calls getUsage with the projectId prop", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView projectId={1} />);

    await waitFor(() => expect(api.getUsage).toHaveBeenCalled());
    expect(api.getUsage.mock.calls.at(-1)[1]).toBe(1);
  });

  it("calls getUsage with an undefined projectId when unscoped", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView />);

    await waitFor(() => expect(api.getUsage).toHaveBeenCalled());
    expect(api.getUsage.mock.calls.at(-1)[1]).toBeUndefined();
  });
});

describe("UsageView excluded-operational-jobs caption", () => {
  it("renders the excluded-jobs caption when the response includes exclusion fields", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue({
      ...BASE_USAGE,
      excludes_operational: true,
      operational_jobs_excluded: 3,
    });

    render(<UsageView />);

    expect(await screen.findByText(/Excludes 3 operational auto-deploy jobs/)).toBeInTheDocument();
  });

  it("renders no caption and does not crash when exclusion fields are absent", async () => {
    const api = await import("../api.js");
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView />);

    expect(await screen.findByText(/Interactive dev sessions \(Claude Code\)/)).toBeInTheDocument();
    expect(screen.queryByText(/Excludes/)).not.toBeInTheDocument();
  });
});

describe("UsageView three-state rendering", () => {
  it("shows the access-denied message when getUsage is forbidden", async () => {
    const api = await import("../api.js");
    api.getUsage.mockRejectedValue(new Error("forbidden"));

    render(<UsageView />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
  });

  it("shows an error message with a working Retry on a generic fetch failure", async () => {
    const api = await import("../api.js");
    api.getUsage.mockRejectedValueOnce(new Error("network down"));
    api.getUsage.mockResolvedValue(BASE_USAGE);

    render(<UsageView />);

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);

    await waitFor(() =>
      expect(screen.getByText(/Interactive dev sessions \(Claude Code\)/)).toBeInTheDocument()
    );
  });
});
