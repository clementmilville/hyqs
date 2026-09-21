import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, within, act } from "@testing-library/react";
import { AdminVisitorAnalytics } from "./AdminVisitorAnalytics.jsx";

vi.mock("../api.js", () => ({
  getPageViewsSummary: vi.fn(),
}));

const BASE_PAGE_VIEWS = {
  totals: {
    views: 12,
    unique_visitors: 5,
    avg_pages_per_visitor: 2.4,
    days_with_traffic: 2,
    last_seen: "2026-09-06T12:00:00Z",
  },
  by_day: [{ day: "2026-09-06", views: 8, unique_visitors: 4 }],
  by_country: [
    { country: "US", views: 8, unique_visitors: 4 },
    { country: "FR", views: 3, unique_visitors: 2 },
  ],
  by_city: [
    { country: "US", city: "New York", views: 5 },
    { country: "FR", city: "Paris", views: 3 },
  ],
  by_ref: [
    { ref: "(no token)", views: 2, unique_visitors: 1 },
    { ref: "newsletter", views: 6, unique_visitors: 3 },
  ],
  by_referrer: [{ referrer_host: "google.com", views: 5 }],
  by_device: [{ os_family: "macOS", ua_family: "Chrome", views: 7, unique_visitors: 3 }],
  by_path: [
    { path: "/how-it-works", views: 8, unique_visitors: 4 },
    { path: "/how-it-works/deploy", views: 4, unique_visitors: 2 },
  ],
  by_hour: Array.from({ length: 24 }, (_, hour) => ({ hour, views: hour === 12 ? 8 : 0 })),
  pages_per_visitor: [
    { pages: 1, visitors: 3 },
    { pages: 2, visitors: 1 },
  ],
  returning: { single_day: 4, multi_day: 1 },
  by_viewport: [{ viewport: "", views: 3, unique_visitors: 2 }],
  by_color_scheme: [{ color_scheme: "dark", views: 9 }],
  by_lang: [{ lang: "en-US", views: 10, unique_visitors: 4 }],
  by_os_version: [{ os_family: "macOS", os_version: "unknown", views: 7 }],
  recent: [
    {
      ts: "2026-09-06T12:00:00Z",
      path: "/how-it-works",
      ref: "newsletter",
      country: "US",
      city: "New York",
      os_family: "macOS",
      ua_family: "Chrome",
      lang: "en-US",
      screen: "desktop",
      visitor_hash: "abcd1234hash",
    },
  ],
};

describe("AdminVisitorAnalytics", () => {
  beforeEach(async () => {
    const api = await import("../api.js");
    vi.clearAllMocks();
    api.getPageViewsSummary.mockResolvedValue(BASE_PAGE_VIEWS);
  });

  it("renders KPI tiles including average pages per visitor and the Pages table", async () => {
    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText("Avg pages per visitor")).toBeInTheDocument();
    expect(screen.getByText("2.4")).toBeInTheDocument();
    for (const label of ["Unique visitors", "Views", "Days with traffic", "Last visit"]) {
      expect(screen.getAllByText(label).length).toBeGreaterThan(0);
    }
    expect(screen.getAllByText("Pages").length).toBeGreaterThan(0);
    expect(screen.getAllByText("/how-it-works").length).toBeGreaterThan(0);
    expect(screen.getByText("/how-it-works/deploy")).toBeInTheDocument();
  });

  it("defaults average pages per visitor to 0 when absent", async () => {
    const api = await import("../api.js");
    const totalsWithoutAvg = { ...BASE_PAGE_VIEWS.totals };
    delete totalsWithoutAvg.avg_pages_per_visitor;
    api.getPageViewsSummary.mockResolvedValue({
      ...BASE_PAGE_VIEWS,
      totals: totalsWithoutAvg,
    });

    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText("Avg pages per visitor")).toBeInTheDocument();
    const card = screen.getByText("Avg pages per visitor").closest(".perf-stat-card");
    expect(within(card).getByText("0")).toBeInTheDocument();
  });

  it("shows the access-denied message when the API rejects with forbidden", async () => {
    const api = await import("../api.js");
    api.getPageViewsSummary.mockRejectedValue(new Error("forbidden"));

    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText(/don.t have access to this data/i)).toBeInTheDocument();
  });

  it("shows the empty state when totals.views is 0", async () => {
    const api = await import("../api.js");
    api.getPageViewsSummary.mockResolvedValue({
      ...BASE_PAGE_VIEWS,
      totals: { ...BASE_PAGE_VIEWS.totals, views: 0 },
    });

    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText("No page views in this range.")).toBeInTheDocument();
  });

  it("re-calls getPageViewsSummary when a range preset or the path filter changes", async () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-09-06T12:00:00Z"));
    const api = await import("../api.js");

    render(<AdminVisitorAnalytics />);
    await act(async () => {});
    expect(api.getPageViewsSummary).toHaveBeenLastCalledWith({
      since: "2026-08-07T12:00:00.000Z",
      until: "2026-09-06T12:00:00.000Z",
    });

    fireEvent.click(screen.getByRole("button", { name: "7d" }));
    await act(async () => {});
    expect(api.getPageViewsSummary).toHaveBeenLastCalledWith({
      since: "2026-08-30T12:00:00.000Z",
      until: "2026-09-06T12:00:00.000Z",
    });

    fireEvent.change(screen.getByLabelText("Path (optional)"), { target: { value: "/pricing" } });
    await act(async () => {});
    expect(api.getPageViewsSummary).toHaveBeenLastCalledWith({
      since: "2026-08-30T12:00:00.000Z",
      until: "2026-09-06T12:00:00.000Z",
      path: "/pricing",
    });
    vi.useRealTimers();
  });

  it("renders the recent list with no visitor hash anywhere in the DOM", async () => {
    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText("Recent visits")).toBeInTheDocument();
    expect(screen.getByText(/no IP address is stored/i)).toBeInTheDocument();
    expect(screen.queryByText(/abcd1234hash/)).not.toBeInTheDocument();
  });

  it("expanding a country reveals only that country's cities", async () => {
    render(<AdminVisitorAnalytics />);

    await screen.findByText("Where");
    expect(screen.queryByText("New York")).not.toBeInTheDocument();
    expect(screen.queryByText("Paris")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("US", { selector: ".u-stage-source" }));

    expect(await screen.findByText("New York")).toBeInTheDocument();
    expect(screen.queryByText("Paris")).not.toBeInTheDocument();

    fireEvent.click(screen.getByText("FR", { selector: ".u-stage-source" }));

    expect(await screen.findByText("Paris")).toBeInTheDocument();
  });

  it("renders referrer and by-ref tables, the latter showing (no token)", async () => {
    render(<AdminVisitorAnalytics />);

    expect(await screen.findByText("Referrers")).toBeInTheDocument();
    expect(screen.getByText("google.com")).toBeInTheDocument();
    expect(screen.getByText("Who sent them")).toBeInTheDocument();
    expect(screen.getByText("(no token)")).toBeInTheDocument();
    expect(screen.getByText("newsletter")).toBeInTheDocument();
  });

  it("renders a blank dimension value as unknown", async () => {
    render(<AdminVisitorAnalytics />);

    expect((await screen.findAllByText("Viewport")).length).toBeGreaterThan(0);
    expect(screen.getByText("unknown")).toBeInTheDocument();
  });
});
