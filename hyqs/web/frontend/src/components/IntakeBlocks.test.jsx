import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import IntakeBlocks from "./IntakeBlocks.jsx";

describe("IntakeBlocks", () => {
  it("renders all three sections when full blocks are provided", () => {
    const blocks = {
      summary: "A brief summary",
      body: "The main body text",
      action_items: ["Do this", "Do that"],
    };
    render(<IntakeBlocks blocks={blocks} />);
    expect(screen.getByText("A brief summary")).toBeInTheDocument();
    expect(screen.getByText("The main body text")).toBeInTheDocument();
    expect(screen.getByText("Do this")).toBeInTheDocument();
    expect(screen.getByText("Do that")).toBeInTheDocument();
  });

  it("renders only summary when body and action_items are absent", () => {
    const blocks = { summary: "Just a summary", body: null, action_items: [] };
    const { container } = render(<IntakeBlocks blocks={blocks} />);
    expect(screen.getByText("Just a summary")).toBeInTheDocument();
    expect(container.querySelector(".intake-block-body")).toBeNull();
    expect(container.querySelector(".intake-block-actions")).toBeNull();
  });

  it("renders nothing when all blocks are empty", () => {
    const { container } = render(
      <IntakeBlocks blocks={{ summary: null, body: null, action_items: [] }} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when blocks prop is null", () => {
    const { container } = render(<IntakeBlocks blocks={null} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders summary card with distinct class", () => {
    const blocks = { summary: "Summary text", body: null, action_items: [] };
    const { container } = render(<IntakeBlocks blocks={blocks} />);
    expect(container.querySelector(".intake-block-summary")).toBeInTheDocument();
  });
});
