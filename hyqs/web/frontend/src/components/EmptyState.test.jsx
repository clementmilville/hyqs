import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ClipboardList } from "lucide-react";
import { EmptyState } from "./EmptyState.jsx";

describe("EmptyState", () => {
  it("renders a string emoji icon (back-compat)", () => {
    const { container } = render(<EmptyState icon="📋" title="No jobs" />);
    expect(screen.getByText("📋")).toBeInTheDocument();
    expect(container.querySelector("svg")).not.toBeInTheDocument();
  });

  it("renders a lucide-react icon component", () => {
    const { container } = render(<EmptyState icon={ClipboardList} title="No jobs" />);
    expect(container.querySelector("svg")).toBeInTheDocument();
    expect(screen.queryByText("📋")).not.toBeInTheDocument();
  });

  it("renders title and hint regardless of icon type", () => {
    render(<EmptyState icon={ClipboardList} title="No jobs" hint="Try again later." />);
    expect(screen.getByText("No jobs")).toBeInTheDocument();
    expect(screen.getByText("Try again later.")).toBeInTheDocument();
  });

  it("renders no icon element when icon is omitted", () => {
    const { container } = render(<EmptyState title="No jobs" />);
    expect(container.querySelector(".empty-icon")).not.toBeInTheDocument();
  });
});
