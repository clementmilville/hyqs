import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { JobOverviewTab } from "./JobOverviewTab.jsx";

const JOB_BASE = {
  id: 1,
  idea: "test idea",
  title: "Test Job",
  status: "done",
  stage: null,
  started_at: null,
  ended_at: null,
  error: null,
  archived: false,
};

describe("JobOverviewTab implementation_summary", () => {
  it("renders What shipped section when implementation_summary is set", () => {
    const job = { ...JOB_BASE, implementation_summary: "Implemented X." };
    render(<JobOverviewTab job={job} events={[]} />);
    expect(screen.getByText("What shipped")).toBeInTheDocument();
    expect(screen.getByText("Implemented X.")).toBeInTheDocument();
  });

  it("hides What shipped section when implementation_summary is absent", () => {
    render(<JobOverviewTab job={JOB_BASE} events={[]} />);
    expect(screen.queryByText("What shipped")).not.toBeInTheDocument();
  });
});

describe("JobOverviewTab status badge", () => {
  it("renders the warn-toned unverified badge for a done job with already-satisfied resolution", () => {
    const job = { ...JOB_BASE, status: "done", resolution: "already-satisfied" };
    render(<JobOverviewTab job={job} events={[]} />);
    const badge = screen.getByText("unverified");
    expect(badge.className).toMatch(/badge warn/);
  });

  it("renders the plain status badge for a normal done job", () => {
    render(<JobOverviewTab job={JOB_BASE} events={[]} />);
    expect(screen.getByText("done")).toBeInTheDocument();
  });
});

describe("JobOverviewTab Filed via", () => {
  it("renders truncated actor name with full email in title when source_actor is an email", () => {
    const job = { ...JOB_BASE, source: "ui", source_actor: "jane@example.com" };
    render(<JobOverviewTab job={job} events={[]} />);
    expect(screen.getByText("Filed via")).toBeInTheDocument();
    const badge = screen.getByText("ui");
    expect(screen.getByText("· jane")).toBeInTheDocument();
    expect(badge.closest(".source-badge").title).toContain("jane@example.com");
  });

  it("renders only the channel badge when source_actor is absent", () => {
    const job = { ...JOB_BASE, source: "supervisor", source_actor: null };
    render(<JobOverviewTab job={job} events={[]} />);
    expect(screen.getByText("supervisor")).toBeInTheDocument();
    expect(screen.queryByText(/·/)).not.toBeInTheDocument();
  });

  it("falls back to unknown when source is absent", () => {
    const job = { ...JOB_BASE, source: null, source_actor: null };
    render(<JobOverviewTab job={job} events={[]} />);
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });
});
