import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { DecisionsList } from "./DecisionsList.jsx";
import { listDecisions, getDecision } from "../api.js";

vi.mock("../api.js", () => ({
  listDecisions: vi.fn(),
  getDecision: vi.fn(),
}));

describe("DecisionsList", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("renders the empty state when there are no decisions", async () => {
    listDecisions.mockResolvedValue([]);
    render(<DecisionsList projectId={1} />);

    await waitFor(() =>
      expect(
        screen.getByText("Decisions are recorded automatically when jobs merge.")
      ).toBeInTheDocument()
    );
  });

  it("renders a row per decision with job id, title, and date", async () => {
    listDecisions.mockResolvedValue([
      {
        filename: "2-second.md",
        job_id: 2,
        title: "Job #2: Second",
        date: "2026-07-02",
        first_line: "did B",
      },
      {
        filename: "1-first.md",
        job_id: 1,
        title: "Job #1: First",
        date: "2026-07-01",
        first_line: "did A",
      },
    ]);
    render(<DecisionsList projectId={1} />);

    await waitFor(() => expect(screen.getByText("Job #2: Second")).toBeInTheDocument());
    expect(screen.getByText("#2")).toBeInTheDocument();
    expect(screen.getByText("2026-07-02")).toBeInTheDocument();
    expect(screen.getByText("Job #1: First")).toBeInTheDocument();
  });

  it("clicking a row fetches and renders the decision markdown", async () => {
    listDecisions.mockResolvedValue([
      {
        filename: "1-first.md",
        job_id: 1,
        title: "Job #1: First",
        date: "2026-07-01",
        first_line: "did A",
      },
    ]);
    getDecision.mockResolvedValue({
      filename: "1-first.md",
      content: "## Details\n\nThe full story.",
    });
    render(<DecisionsList projectId={1} />);

    await waitFor(() => expect(screen.getByText("Job #1: First")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /Job #1: First/ }));

    await waitFor(() => expect(getDecision).toHaveBeenCalledWith(1, "1-first.md"));
    await waitFor(() => expect(screen.getByText("The full story.")).toBeInTheDocument());
  });

  it("expanding twice only calls getDecision once (cached)", async () => {
    listDecisions.mockResolvedValue([
      {
        filename: "1-first.md",
        job_id: 1,
        title: "Job #1: First",
        date: "2026-07-01",
        first_line: "did A",
      },
    ]);
    getDecision.mockResolvedValue({ filename: "1-first.md", content: "cached content" });
    render(<DecisionsList projectId={1} />);

    await waitFor(() => expect(screen.getByText("Job #1: First")).toBeInTheDocument());
    const row = screen.getByRole("button", { name: /Job #1: First/ });

    fireEvent.click(row); // expand -> fetch
    await waitFor(() => expect(screen.getByText("cached content")).toBeInTheDocument());

    fireEvent.click(row); // collapse
    fireEvent.click(row); // expand again -> should use cache

    await waitFor(() => expect(screen.getByText("cached content")).toBeInTheDocument());
    expect(getDecision).toHaveBeenCalledTimes(1);
  });

  it("passes epicId through to listDecisions", async () => {
    listDecisions.mockResolvedValue([]);
    render(<DecisionsList projectId={1} epicId={9} />);

    await waitFor(() => expect(listDecisions).toHaveBeenCalledWith(1, 9));
  });

  it("shows the Spinner while the fetch is pending", async () => {
    let resolveFetch;
    listDecisions.mockReturnValue(
      new Promise((resolve) => {
        resolveFetch = resolve;
      })
    );
    render(<DecisionsList projectId={1} />);

    expect(document.querySelector(".spinner")).not.toBeNull();

    resolveFetch([]);
    await waitFor(() =>
      expect(
        screen.getByText("Decisions are recorded automatically when jobs merge.")
      ).toBeInTheDocument()
    );
  });

  it("shows the access-denied message when listDecisions is forbidden", async () => {
    listDecisions.mockRejectedValue(new Error("forbidden"));
    render(<DecisionsList projectId={1} />);

    await waitFor(() =>
      expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument()
    );
  });

  it("shows an error message with Retry, and Retry re-invokes listDecisions", async () => {
    listDecisions.mockRejectedValueOnce(new Error("boom"));
    listDecisions.mockResolvedValueOnce([]);
    render(<DecisionsList projectId={1} />);

    await waitFor(() =>
      expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument()
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(
        screen.getByText("Decisions are recorded automatically when jobs merge.")
      ).toBeInTheDocument()
    );
  });
});
