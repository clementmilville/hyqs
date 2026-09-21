import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { EventDetail } from "./EventDetail.jsx";

describe("EventDetail security stage", () => {
  it("shows the final failed verdict and deterministic findings", () => {
    render(
      <EventDetail
        ev={{
          stage: "security",
          status: "failed",
          summary: "Security gate failed: 1 deterministic scanner finding(s).",
          detail: {
            verdict: "fail",
            findings: [
              {
                severity: "high",
                note: "[detect-secrets] Potential secret detected: Secret Keyword",
                file: "backend/tests/test_config.py",
              },
            ],
          },
        }}
        jobId={3374}
        onViewDiff={() => {}}
      />,
    );

    expect(screen.getByText(/Security gate failed/)).toBeInTheDocument();
    expect(screen.getByText("Final verdict: fail")).toBeInTheDocument();
    expect(screen.getByText(/Potential secret detected/)).toBeInTheDocument();
    expect(screen.getByText(/backend\/tests\/test_config.py/)).toBeInTheDocument();
  });
});
