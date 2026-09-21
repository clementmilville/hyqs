import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ThinkingIndicator } from "./ThinkingIndicator.jsx";

describe("ThinkingIndicator", () => {
  it("renders 'Working…' text when visible=true", () => {
    render(<ThinkingIndicator visible={true} />);
    expect(screen.getByText("Working…")).toBeInTheDocument();
  });

  it("renders the pulsing dot when visible=true", () => {
    const { container } = render(<ThinkingIndicator visible={true} />);
    expect(container.querySelector(".thinking-dot")).toBeInTheDocument();
  });

  it("renders nothing when visible=false", () => {
    const { container } = render(<ThinkingIndicator visible={false} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders nothing when visible is not passed", () => {
    const { container } = render(<ThinkingIndicator />);
    expect(container.firstChild).toBeNull();
  });
});
