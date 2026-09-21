import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { JobDependenciesTab } from "./JobDependenciesTab.jsx";
import { getJobDependencyJobs, getJobDependents } from "../api.js";

vi.mock("../api.js", () => ({
  getJobDependencyJobs: vi.fn(),
  getJobDependents: vi.fn(),
}));

const job = { id: 10, title: "Parent job" };

describe("JobDependenciesTab", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("shows a Spinner while both fetches are pending", async () => {
    let resolveDependsOn;
    getJobDependencyJobs.mockReturnValue(
      new Promise((resolve) => {
        resolveDependsOn = resolve;
      })
    );
    getJobDependents.mockReturnValue(new Promise(() => {}));
    render(<JobDependenciesTab job={job} onOpenJob={vi.fn()} />);

    expect(document.querySelectorAll(".spinner").length).toBeGreaterThan(0);

    resolveDependsOn([]);
    await waitFor(() => expect(getJobDependencyJobs).toHaveBeenCalledWith(10));
  });

  it("renders hint text for both sections when both lists are empty", async () => {
    getJobDependencyJobs.mockResolvedValue([]);
    getJobDependents.mockResolvedValue([]);
    render(<JobDependenciesTab job={job} onOpenJob={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("No dependencies.")).toBeInTheDocument());
    expect(screen.getByText("No dependent jobs.")).toBeInTheDocument();
  });

  it("renders rows for both sections when jobs are returned", async () => {
    getJobDependencyJobs.mockResolvedValue([
      { id: 1, title: "Upstream job", status: "done" },
    ]);
    getJobDependents.mockResolvedValue([{ id: 2, title: "Downstream job", status: "pending" }]);
    render(<JobDependenciesTab job={job} onOpenJob={vi.fn()} />);

    await waitFor(() => expect(screen.getByText("Upstream job")).toBeInTheDocument());
    expect(screen.getByText("#1")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();

    expect(screen.getByText("Downstream job")).toBeInTheDocument();
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();
  });

  it("clicking a depends-on row calls onOpenJob with that job's id", async () => {
    getJobDependencyJobs.mockResolvedValue([
      { id: 1, title: "Upstream job", status: "done" },
    ]);
    getJobDependents.mockResolvedValue([]);
    const onOpenJob = vi.fn();
    render(<JobDependenciesTab job={job} onOpenJob={onOpenJob} />);

    await waitFor(() => expect(screen.getByText("Upstream job")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Upstream job"));

    expect(onOpenJob).toHaveBeenCalledWith(1);
  });

  it("clicking a dependents row calls onOpenJob with that job's id", async () => {
    getJobDependencyJobs.mockResolvedValue([]);
    getJobDependents.mockResolvedValue([{ id: 2, title: "Downstream job", status: "pending" }]);
    const onOpenJob = vi.fn();
    render(<JobDependenciesTab job={job} onOpenJob={onOpenJob} />);

    await waitFor(() => expect(screen.getByText("Downstream job")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Downstream job"));

    expect(onOpenJob).toHaveBeenCalledWith(2);
  });

  it("shows the access-denied message when a fetch is forbidden", async () => {
    getJobDependencyJobs.mockRejectedValue(new Error("forbidden"));
    getJobDependents.mockResolvedValue([]);
    render(<JobDependenciesTab job={job} onOpenJob={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument()
    );
  });

  it("shows an error message with Retry when a fetch fails", async () => {
    getJobDependencyJobs.mockRejectedValueOnce(new Error("boom"));
    getJobDependencyJobs.mockResolvedValueOnce([]);
    getJobDependents.mockResolvedValue([]);
    render(<JobDependenciesTab job={job} onOpenJob={vi.fn()} />);

    await waitFor(() =>
      expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getByText("No dependencies.")).toBeInTheDocument());
  });
});
