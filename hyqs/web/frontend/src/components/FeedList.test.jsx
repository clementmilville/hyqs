import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { FeedList, groupByDay, formatDayHeader } from "./FeedList.jsx";

describe("groupByDay", () => {
  it("groups items by calendar day, preserving order within and across groups", () => {
    const items = [
      { id: 1, at: "2026-01-15T10:00:00Z" },
      { id: 2, at: "2026-01-15T18:00:00Z" },
      { id: 3, at: "2026-01-14T09:00:00Z" },
    ];

    const groups = groupByDay(items, (i) => i.at);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ day: "2026-01-15", items: [items[0], items[1]] });
    expect(groups[1]).toMatchObject({ day: "2026-01-14", items: [items[2]] });
  });

  it("buckets a missing/invalid date under 'unknown'", () => {
    const items = [
      { id: 1, at: null },
      { id: 2, at: "not-a-date" },
    ];

    const groups = groupByDay(items, (i) => i.at);

    expect(groups).toHaveLength(1);
    expect(groups[0].day).toBe("unknown");
  });
});

describe("formatDayHeader", () => {
  it("labels the unknown bucket distinctly from a real date", () => {
    expect(formatDayHeader("unknown")).toBe("Unknown date");
    expect(formatDayHeader("2026-01-15")).toMatch(/15 Jan 2026/);
  });
});

describe("FeedList", () => {
  it("renders one header per day and delegates rows to renderRow", () => {
    const items = [
      { id: 1, at: "2026-01-15T10:00:00Z", title: "First" },
      { id: 2, at: "2026-01-14T09:00:00Z", title: "Second" },
    ];

    render(
      <FeedList
        items={items}
        getDate={(i) => i.at}
        renderRow={(i) => <div key={i.id}>{i.title}</div>}
      />
    );

    expect(screen.getByText(/15 Jan 2026/)).toBeInTheDocument();
    expect(screen.getByText(/14 Jan 2026/)).toBeInTheDocument();
    expect(screen.getByText("First")).toBeInTheDocument();
    expect(screen.getByText("Second")).toBeInTheDocument();
  });

  it("renders a coalesced-burst count chip per day group via renderCountChip", () => {
    const items = [
      { id: 1, at: "2026-01-15T10:00:00Z" },
      { id: 2, at: "2026-01-15T18:00:00Z" },
    ];

    render(
      <FeedList
        items={items}
        getDate={(i) => i.at}
        renderRow={(i) => <div key={i.id} />}
        renderCountChip={(group) => <span>{group.items.length} items</span>}
      />
    );

    expect(screen.getByText("2 items")).toBeInTheDocument();
  });

  it("renders nothing for an empty list when no emptyHint is given", () => {
    const { container } = render(
      <FeedList items={[]} getDate={() => null} renderRow={() => null} />
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders emptyHint text when items is empty and emptyHint is provided", () => {
    render(
      <FeedList items={[]} getDate={() => null} renderRow={() => null} emptyHint="Nothing yet." />
    );

    expect(screen.getByText("Nothing yet.")).toBeInTheDocument();
  });
});
