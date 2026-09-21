import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { EpicsIndex } from "./EpicsIndex.jsx";

vi.mock("../api.js", () => ({
  createEpic: vi.fn(() => Promise.resolve({})),
  archiveEpic: vi.fn(() => Promise.resolve()),
  unarchiveEpic: vi.fn(() => Promise.resolve()),
}));

const project = { id: 1, repo_path: "/repo" };

const epics = [
  {
    id: 1,
    name: "Checkout revamp",
    archived: false,
    status: "active",
    job_count: 3,
    last_activity: new Date().toISOString(),
    status_counts: { done: 1, running: 1, failed: 1 },
  },
];

function renderIndex(props = {}) {
  return render(
    <EpicsIndex
      epics={epics}
      project={project}
      onChanged={vi.fn()}
      setNote={vi.fn()}
      showArchivedEpics={false}
      setShowArchivedEpics={vi.fn()}
      onOpenEpic={vi.fn()}
      onOpenIdeate={vi.fn()}
      onOpenArchitect={vi.fn()}
      ideateBusyEpicId={null}
      architectBusyEpicId={null}
      {...props}
    />
  );
}

describe("EpicsIndex", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  it("shows one row per epic with name, status, and a done/running/failed progress summary", () => {
    renderIndex();
    expect(screen.getByText("Checkout revamp")).toBeInTheDocument();
    expect(screen.getByText("active")).toBeInTheDocument();
    expect(screen.getByText("1 done · 1 running · 1 failed")).toBeInTheDocument();
  });

  it("clicking the row opens the epic", () => {
    const onOpenEpic = vi.fn();
    renderIndex({ onOpenEpic });
    fireEvent.click(screen.getByTitle("Open epic"));
    expect(onOpenEpic).toHaveBeenCalledWith(1);
  });

  it("clicking ideate/architect quick actions calls through", () => {
    const onOpenIdeate = vi.fn();
    const onOpenArchitect = vi.fn();
    renderIndex({ onOpenIdeate, onOpenArchitect });
    fireEvent.click(screen.getByTitle("Suggest features"));
    expect(onOpenIdeate).toHaveBeenCalledWith(epics[0]);
    fireEvent.click(screen.getByTitle("Architect: plan this epic"));
    expect(onOpenArchitect).toHaveBeenCalledWith(epics[0]);
  });

  it("ideate/architect quick actions render a lucide icon plus a visible text label, no emoji", () => {
    const { container } = renderIndex();
    expect(screen.getByText("Ideate")).toBeInTheDocument();
    expect(screen.getByText("Architect")).toBeInTheDocument();
    expect(container.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}☀-➿]/u);
    expect(screen.getByTitle("Suggest features").querySelector("svg")).toBeInTheDocument();
    expect(screen.getByTitle("Architect: plan this epic").querySelector("svg")).toBeInTheDocument();
  });

  it("empty state renders a lucide icon, not an emoji", () => {
    const { container } = renderIndex({ epics: [] });
    expect(screen.getByText("No epics yet")).toBeInTheDocument();
    expect(container.querySelector(".empty-icon")?.tagName.toLowerCase()).toBe("svg");
  });

  it("sorts epics attention-first: failed before running before done-only", () => {
    const baseCounts = { done: 3, running: 0, failed: 0 };
    const threeEpics = [
      { ...epics[0], id: 1, name: "Done epic", status_counts: { ...baseCounts } },
      { ...epics[0], id: 2, name: "Running epic", status_counts: { ...baseCounts, running: 1 } },
      { ...epics[0], id: 3, name: "Failed epic", status_counts: { ...baseCounts, failed: 1 } },
    ];
    renderIndex({ epics: threeEpics });

    const rows = screen.getAllByTitle("Open epic");
    const names = rows.map((r) => r.querySelector(".epics-index-name").textContent);
    expect(names).toEqual(["Failed epic", "Running epic", "Done epic"]);
  });

  it("creates a new epic from the inline form", async () => {
    const onChanged = vi.fn();
    renderIndex({ onChanged });
    fireEvent.change(screen.getByPlaceholderText("New epic…"), { target: { value: "Onboarding" } });
    fireEvent.click(screen.getByText("New epic"));
    const api = await import("../api.js");
    expect(api.createEpic).toHaveBeenCalledWith(project.id, "Onboarding", "");
  });

  it("hides archived epics until the toggle is used", () => {
    const archivedEpics = [...epics, { ...epics[0], id: 2, name: "Old thing", archived: true }];
    renderIndex({ epics: archivedEpics, showArchivedEpics: false });
    expect(screen.queryByText("Old thing")).not.toBeInTheDocument();

    renderIndex({ epics: archivedEpics, showArchivedEpics: true });
    expect(screen.getByText("Old thing")).toBeInTheDocument();
  });

  it("Archive lives behind a row menu and a confirm — never a bare, loud button", async () => {
    const onChanged = vi.fn();
    renderIndex({ onChanged });

    // Not visible until the row menu is opened.
    expect(screen.queryByText("Archive")).not.toBeInTheDocument();

    fireEvent.click(screen.getByLabelText("Epic actions"));
    const archiveBtn = screen.getByText("Archive");
    fireEvent.click(archiveBtn);

    expect(window.confirm).toHaveBeenCalled();
    const api = await import("../api.js");
    expect(api.archiveEpic).toHaveBeenCalledWith(1);
  });
});
