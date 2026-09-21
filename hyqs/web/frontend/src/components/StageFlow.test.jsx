import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { StageFlow } from "./StageFlow.jsx";
import { STAGE_DONE } from "../constants.js";

describe("10 nodes render", () => {
  it("renders all 10 step nodes", () => {
    render(<StageFlow job={{ stage: "queued", status: "pending", attempts: 0 }} />);
    for (const step of [
      "plan",
      "build",
      "lint",
      "test",
      "review",
      "security",
      "design_review",
      "merge",
      "deploy",
      "done",
    ]) {
      expect(screen.getByTitle(step)).toBeInTheDocument();
    }
  });
});

describe("monotonic STAGE_DONE", () => {
  it("never decreases across the real stage progression", () => {
    const progression = [
      "queued",
      "plan",
      "lint",
      "build",
      "test",
      "review",
      "security",
      "design_review",
      "deploy",
      "done",
    ];
    let prev = -1;
    for (const stage of progression) {
      const val = STAGE_DONE[stage];
      expect(val).toBeGreaterThanOrEqual(prev);
      prev = val;
    }
  });

  it("maps lint to 2 (lint active) and deploy to 8 (deploy active)", () => {
    expect(STAGE_DONE["lint"]).toBe(2);
    expect(STAGE_DONE["deploy"]).toBe(8);
  });
});

describe("deploy node active while stage=deploy", () => {
  it("marks the deploy node as active when status is running", () => {
    render(<StageFlow job={{ stage: "deploy", status: "running", attempts: 0 }} />);
    expect(screen.getByTitle("deploy").className).toContain("active");
  });
});

describe("design_review node active while stage transitions", () => {
  it("marks design_review active (not merge) while stage=security", () => {
    render(<StageFlow job={{ stage: "security", status: "running", attempts: 0 }} />);
    expect(screen.getByTitle("design_review").className).toContain("active");
    expect(screen.getByTitle("merge").className).not.toContain("active");
  });

  it("marks merge active while stage=design_review", () => {
    render(<StageFlow job={{ stage: "design_review", status: "running", attempts: 0 }} />);
    expect(screen.getByTitle("merge").className).toContain("active");
  });
});

describe("fix loop-back", () => {
  it("highlights build node and shows fixing badge while stage=fix", () => {
    render(<StageFlow job={{ stage: "fix", status: "running", attempts: 2 }} />);
    expect(screen.getByTitle("build").className).toContain("active");
    const badge = screen.getByTitle("2 self-heal round(s)");
    expect(badge.textContent).toContain("🔁 ×2");
    expect(badge.textContent).toContain("fixing…");
  });

  it("keeps attempt counter visible after fix advances to lint", () => {
    render(<StageFlow job={{ stage: "lint", status: "running", attempts: 2 }} />);
    expect(screen.getByTitle("lint").className).toContain("active");
    expect(screen.getByTitle("2 self-heal round(s)")).toBeInTheDocument();
  });
});
