import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { JobCostTab } from "./JobCostTab.jsx";
import { getJobUsage } from "../api.js";

vi.mock("../api.js", () => ({
  getJobUsage: vi.fn(),
}));

const job = { id: 692, title: "Some job" };

describe("JobCostTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a Spinner while the fetch is pending", async () => {
    let resolveUsage;
    getJobUsage.mockReturnValue(
      new Promise((resolve) => {
        resolveUsage = resolve;
      })
    );
    render(<JobCostTab job={job} />);

    expect(document.querySelectorAll(".spinner").length).toBeGreaterThan(0);

    resolveUsage([]);
    await waitFor(() => expect(getJobUsage).toHaveBeenCalledWith(692));
  });

  it("renders a row per usage group plus a correctly summed total row", async () => {
    getJobUsage.mockResolvedValue([
      {
        source: "build",
        model: "claude-sonnet-4-6",
        provider: "claude",
        input_tokens: 1000,
        output_tokens: 200,
        cost_usd: 0.5,
      },
      {
        source: "review",
        model: "gpt-5",
        provider: "codex",
        input_tokens: 500,
        output_tokens: 100,
        cost_usd: 0.25,
      },
    ]);
    render(<JobCostTab job={job} />);

    await waitFor(() => expect(screen.getByText("build")).toBeInTheDocument());
    expect(screen.getByText("claude-sonnet-4-6")).toBeInTheDocument();
    expect(screen.getByText("claude")).toBeInTheDocument();
    expect(screen.getByText("1000")).toBeInTheDocument();
    expect(screen.getByText("200")).toBeInTheDocument();
    expect(screen.getByText("$0.5000")).toBeInTheDocument();

    expect(screen.getByText("review")).toBeInTheDocument();
    expect(screen.getByText("gpt-5")).toBeInTheDocument();
    expect(screen.getByText("codex")).toBeInTheDocument();
    expect(screen.getByText("500")).toBeInTheDocument();
    expect(screen.getByText("100")).toBeInTheDocument();
    expect(screen.getByText("$0.2500")).toBeInTheDocument();

    expect(screen.getByText("Total")).toBeInTheDocument();
    expect(screen.getByText("1500")).toBeInTheDocument();
    expect(screen.getByText("300")).toBeInTheDocument();
    expect(screen.getByText("$0.7500")).toBeInTheDocument();
  });

  it("shows the empty-state message when no usage is recorded", async () => {
    getJobUsage.mockResolvedValue([]);
    render(<JobCostTab job={job} />);

    await waitFor(() =>
      expect(screen.getByText("No usage recorded yet.")).toBeInTheDocument()
    );
  });

  it("shows the access-denied message when the fetch is forbidden", async () => {
    getJobUsage.mockRejectedValue(new Error("forbidden"));
    render(<JobCostTab job={job} />);

    await waitFor(() =>
      expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument()
    );
  });

  it("shows an error message with Retry when the fetch fails", async () => {
    getJobUsage.mockRejectedValueOnce(new Error("boom"));
    getJobUsage.mockResolvedValueOnce([]);
    render(<JobCostTab job={job} />);

    await waitFor(() =>
      expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getByText("No usage recorded yet.")).toBeInTheDocument());
  });
});
