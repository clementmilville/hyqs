import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { PerfJobDetail } from "./PerfJobDetail.jsx";

vi.mock("../../api.js", () => ({
  getJobEvents: vi.fn(),
}));

const JOB = { job_id: 7, title: "Do the thing", status: "done" };

const EVENTS = [
  {
    id: 1,
    stage: "build",
    status: "done",
    started_at: "2026-07-13T00:00:00Z",
    ended_at: "2026-07-13T00:01:00Z",
    attempt: 0,
    tokens: 0,
    cost_usd: 0,
  },
];

describe("PerfJobDetail three-state rendering", () => {
  it("shows a Spinner on the initial load", async () => {
    const api = await import("../../api.js");
    api.getJobEvents.mockReturnValue(new Promise(() => {}));

    render(<PerfJobDetail job={JOB} onBack={() => {}} />);

    expect(document.querySelector(".spinner")).toBeInTheDocument();
  });

  it("shows the access-denied message when getJobEvents is forbidden", async () => {
    const api = await import("../../api.js");
    api.getJobEvents.mockRejectedValue(new Error("forbidden"));

    render(<PerfJobDetail job={JOB} onBack={() => {}} />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
  });

  it("shows an error message with a working Retry on a generic failure", async () => {
    const api = await import("../../api.js");
    api.getJobEvents.mockRejectedValueOnce(new Error("network down"));
    api.getJobEvents.mockResolvedValue(EVENTS);

    render(<PerfJobDetail job={JOB} onBack={() => {}} />);

    const retryBtn = await screen.findByRole("button", { name: /retry/i });
    fireEvent.click(retryBtn);

    await waitFor(() => expect(screen.getByText(/Do the thing/)).toBeInTheDocument());
  });

  it("renders the JobTimeline once events load successfully", async () => {
    const api = await import("../../api.js");
    api.getJobEvents.mockResolvedValue(EVENTS);

    render(<PerfJobDetail job={JOB} onBack={() => {}} />);

    expect(await screen.findByText("#7")).toBeInTheDocument();
    expect(screen.getByText(/Do the thing/)).toBeInTheDocument();
  });
});
