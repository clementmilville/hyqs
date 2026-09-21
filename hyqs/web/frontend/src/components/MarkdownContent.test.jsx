import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { MarkdownContent } from "./MarkdownContent.jsx";

describe("MarkdownContent", () => {
  it("renders markdown as structured HTML", () => {
    const md = "## Heading\n- item one\n- item two\n\n**bold text**\n\nparagraph";
    const { container } = render(<MarkdownContent content={md} />);

    expect(container.querySelector("h2")).not.toBeNull();
    expect(container.querySelector("ul")).not.toBeNull();
    expect(container.querySelector("li")).not.toBeNull();
    expect(container.querySelector("strong")).not.toBeNull();
    expect(container.querySelector("p")).not.toBeNull();
  });

  it("does not render raw HTML tags — XSS guard", () => {
    const { container } = render(<MarkdownContent content='<img src="x" onerror="alert(1)">' />);
    expect(container.querySelector("img")).toBeNull();
  });

  it("handles partial/incomplete markdown without throwing", () => {
    expect(() => render(<MarkdownContent content="**bold" />)).not.toThrow();
    expect(() => render(<MarkdownContent content="" />)).not.toThrow();
  });
});
