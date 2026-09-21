import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StageFlow } from "./StageFlow.jsx";
import { JobOverviewTab } from "./JobOverviewTab.jsx";
import { STEPS } from "../constants.js";

const JOB = {
  id: 1,
  stage: "done",
  status: "done",
  attempts: 2,
  implementation_summary: "Implemented X and Y.",
  idea: "test",
  title: "T",
  started_at: null,
  ended_at: null,
  error: null,
};

describe("done job with implementation_summary", () => {
  it("StageFlow renders all nodes and the fix-round badge", () => {
    render(<StageFlow job={JOB} />);
    for (const step of STEPS) {
      expect(screen.getByTitle(step)).toBeInTheDocument();
    }
    const badge = screen.getByTitle("2 self-heal round(s)");
    expect(badge.textContent).toContain("🔁 ×2");
  });

  it("JobOverviewTab renders the What shipped section", () => {
    render(<JobOverviewTab job={JOB} events={[]} />);
    expect(screen.getByText("What shipped")).toBeInTheDocument();
    expect(screen.getByText("Implemented X and Y.")).toBeInTheDocument();
  });
});
