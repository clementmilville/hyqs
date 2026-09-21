import { describe, it, expect, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { JobDiffTab } from "./JobDiffTab.jsx";
import { getJobDiff } from "../api.js";

vi.mock("../api.js", () => ({
  getJobDiff: vi.fn(),
}));

describe("no branch yet", () => {
  it("renders a friendly hint instead of diff undefined...undefined", async () => {
    getJobDiff.mockResolvedValue({ diff: "", truncated: false, note: "no branch yet" });
    render(<JobDiffTab jobId={1} />);
    await waitFor(() => {
      expect(screen.getByText("No diff yet — this job hasn't branched.")).toBeInTheDocument();
    });
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument();
  });
});

describe("normal diff", () => {
  it("renders the diffhead and pre block unchanged", async () => {
    getJobDiff.mockResolvedValue({
      diff: "some diff text",
      truncated: false,
      base: "main",
      branch: "job-1-x",
    });
    render(<JobDiffTab jobId={1} />);
    await waitFor(() => {
      expect(screen.getByText("diff main...job-1-x")).toBeInTheDocument();
    });
    expect(screen.getByText(/some diff text/)).toBeInTheDocument();
  });
});
