import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ActivityFeed } from "./ActivityFeed.jsx";

function ev(tool, input_summary) {
  return { type: "tool_use", tool, input_summary };
}

describe("ActivityFeed", () => {
  it("renders nothing when events is empty", () => {
    const { container } = render(<ActivityFeed events={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when events is undefined", () => {
    const { container } = render(<ActivityFeed />);
    expect(container.firstChild).toBeNull();
  });

  it("formats Read events as 'Reading {path}'", () => {
    render(<ActivityFeed events={[ev("Read", "Read runner.py")]} />);
    expect(screen.getByText("Reading runner.py")).toBeInTheDocument();
  });

  it("formats Bash events as 'Running {cmd}'", () => {
    render(<ActivityFeed events={[ev("Bash", "Bash pytest tests/")]} />);
    expect(screen.getByText("Running pytest tests/")).toBeInTheDocument();
  });

  it("truncates Bash commands at 60 chars with ellipsis", () => {
    const longCmd = "a".repeat(65);
    render(<ActivityFeed events={[ev("Bash", `Bash ${longCmd}`)]} />);
    expect(screen.getByText(`Running ${"a".repeat(57)}…`)).toBeInTheDocument();
  });

  it("formats Edit events as 'Editing {path}'", () => {
    render(<ActivityFeed events={[ev("Edit", "Edit src/main.py")]} />);
    expect(screen.getByText("Editing src/main.py")).toBeInTheDocument();
  });

  it("formats Write events as 'Editing …'", () => {
    // Backend returns input_summary="Write" for Write (no path available)
    render(<ActivityFeed events={[ev("Write", "Write")]} />);
    expect(screen.getByText(/^Editing/)).toBeInTheDocument();
  });

  it("formats Grep events as 'Grep {pattern}'", () => {
    render(<ActivityFeed events={[ev("Grep", "Grep def foo")]} />);
    expect(screen.getByText("Grep def foo")).toBeInTheDocument();
  });

  it("formats Glob events as 'Glob {pattern}'", () => {
    render(<ActivityFeed events={[ev("Glob", "Glob **/*.py")]} />);
    expect(screen.getByText("Glob **/*.py")).toBeInTheDocument();
  });

  it("renders unknown tools by their tool name", () => {
    render(<ActivityFeed events={[{ type: "tool_use", tool: "WebFetch", input_summary: "" }]} />);
    expect(screen.getByText("WebFetch")).toBeInTheDocument();
  });

  it("shows only the last 12 events when more are given", () => {
    const events = Array.from({ length: 15 }, (_, i) =>
      ev("Read", `Read file${i}.py`)
    );
    render(<ActivityFeed events={events} />);
    // first 3 (file0–file2) should not be visible
    expect(screen.queryByText("Reading file0.py")).not.toBeInTheDocument();
    expect(screen.queryByText("Reading file2.py")).not.toBeInTheDocument();
    // last 12 (file3–file14) should be visible
    expect(screen.getByText("Reading file3.py")).toBeInTheDocument();
    expect(screen.getByText("Reading file14.py")).toBeInTheDocument();
  });
});
