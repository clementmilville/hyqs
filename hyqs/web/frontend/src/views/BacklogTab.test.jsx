import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { BacklogTab } from "./BacklogTab.jsx";
import { RoleContext } from "../context.js";
import { ToastProvider } from "../components/Toast.jsx";

vi.mock("../api.js", () => ({
  listBacklogItems: vi.fn(() => Promise.resolve([])),
  createBacklogItem: vi.fn(),
  voteBacklogItem: vi.fn(),
  patchBacklogItem: vi.fn(),
  refineBacklogItems: vi.fn(() => Promise.resolve({ session_id: "sess-1", proposed_jobs: [] })),
  createBatchJobs: vi.fn(() => Promise.resolve({})),
}));

import { listBacklogItems, createBacklogItem, patchBacklogItem, refineBacklogItems } from "../api.js";

const ITEM = {
  id: 1,
  project_id: 1,
  title: "Sample Feature",
  body: "",
  type: "feature",
  proposed_by: "alice",
  votes: 3,
  status: "new",
  epic_hint: "",
  linked_job_ids: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const ITEM2 = {
  id: 2,
  project_id: 1,
  title: "Another Bug",
  body: "",
  type: "bug",
  proposed_by: "bob",
  votes: 1,
  status: "new",
  epic_hint: "",
  linked_job_ids: [],
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

function renderTab(canFn = () => false) {
  const ctx = {
    role: "viewer",
    can: canFn,
    authLoading: false,
    authError: false,
    retryAuth: () => {},
  };
  return render(
    <RoleContext.Provider value={ctx}>
      <ToastProvider>
        <BacklogTab projectId={1} />
      </ToastProvider>
    </RoleContext.Provider>
  );
}

describe("BacklogTab", () => {
  beforeEach(() => {
    vi.mocked(listBacklogItems).mockResolvedValue([]);
    vi.mocked(refineBacklogItems).mockResolvedValue({
      session_id: "sess-1",
      proposed_jobs: [],
    });
  });

  it("compose button absent without propose_backlog", async () => {
    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    expect(screen.queryByRole("button", { name: "+ Propose" })).toBeNull();
  });

  it("vote buttons absent without propose_backlog", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);

    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    expect(document.querySelector(".vote-btn")).toBeNull();
  });

  it("triage controls absent without triage_backlog", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);

    await act(async () => {
      renderTab((p) => p === "propose_backlog");
    });
    await act(async () => {});

    expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
    expect(screen.queryByRole("button", { name: "Decline" })).toBeNull();
    expect(document.querySelector(".epic-hint-display")).toBeNull();
  });

  it("empty state renders when API returns no items, with a lucide icon (no emoji)", async () => {
    let container;
    await act(async () => {
      ({ container } = renderTab(() => true));
    });
    await act(async () => {});

    expect(screen.getByText("No backlog items")).toBeDefined();
    expect(container.querySelector(".empty-icon")?.tagName.toLowerCase()).toBe("svg");
    expect(container.textContent).not.toMatch(/[\u{1F300}-\u{1FAFF}☀-➿]/u);
  });

  it("shows a spinner while the initial fetch is pending", async () => {
    let resolveFetch;
    vi.mocked(listBacklogItems).mockReturnValueOnce(
      new Promise((resolve) => {
        resolveFetch = resolve;
      })
    );

    let container;
    await act(async () => {
      ({ container } = renderTab(() => true));
    });

    expect(container.querySelector(".spinner")).toBeInTheDocument();
    expect(screen.queryByText("No backlog items")).not.toBeInTheDocument();

    await act(async () => {
      resolveFetch([]);
    });
  });

  it("renders an access-denied message when listBacklogItems rejects with 'forbidden'", async () => {
    vi.mocked(listBacklogItems).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      renderTab(() => true);
    });
    await act(async () => {});

    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
    expect(screen.queryByText("No backlog items")).not.toBeInTheDocument();
  });

  it("renders an error message with a working Retry button on a generic fetch failure", async () => {
    vi.mocked(listBacklogItems).mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      renderTab(() => true);
    });
    await act(async () => {});

    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();
    const retryBtn = screen.getByRole("button", { name: "Retry" });

    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);
    await act(async () => {
      fireEvent.click(retryBtn);
    });
    await act(async () => {});

    expect(screen.getByText("Sample Feature")).toBeInTheDocument();
  });

  it("proposed item appears in list after form submit", async () => {
    const newItem = { ...ITEM, id: 99, title: "Brand New Feature" };
    vi.mocked(createBacklogItem).mockResolvedValueOnce(newItem);

    await act(async () => {
      renderTab((p) => p === "propose_backlog");
    });
    await act(async () => {});

    fireEvent.click(screen.getByRole("button", { name: "+ Propose" }));

    fireEvent.change(screen.getByPlaceholderText("Title"), {
      target: { value: "Brand New Feature" },
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Propose" }));
    });
    await act(async () => {});

    expect(screen.getByText("Brand New Feature")).toBeDefined();
  });

  // --- S5: multi-select → refine entry point tests ---

  it("checkboxes render on each row", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM, ITEM2]);

    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    const rowCheckboxes = screen
      .getAllByRole("checkbox", { name: /^Select / })
      .filter((cb) => cb.getAttribute("aria-label") !== "Select all");
    expect(rowCheckboxes.length).toBe(2);
  });

  it("select-all selects every row visible under the current filter", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM, ITEM2]);

    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    const selectAll = screen.getByRole("checkbox", { name: "Select all" });
    fireEvent.click(selectAll);

    const rowCheckboxes = screen.getAllByRole("checkbox", { name: /^Select / });
    rowCheckboxes.forEach((cb) => expect(cb.checked).toBe(true));

    // Clicking again deselects all
    fireEvent.click(selectAll);
    rowCheckboxes.forEach((cb) => expect(cb.checked).toBe(false));
  });

  it("'Refine selected (N)' label tracks selection count and is disabled at 0", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM, ITEM2]);

    await act(async () => {
      renderTab((p) => p === "queue_job");
    });
    await act(async () => {});

    const refineBtn = screen.getByRole("button", { name: /Refine selected/ });
    expect(refineBtn.textContent).toContain("0");
    expect(refineBtn.disabled).toBe(true);

    fireEvent.click(screen.getByRole("checkbox", { name: `Select ${ITEM.title}` }));
    expect(refineBtn.textContent).toContain("1");
    expect(refineBtn.disabled).toBe(false);

    fireEvent.click(screen.getByRole("checkbox", { name: `Select ${ITEM2.title}` }));
    expect(refineBtn.textContent).toContain("2");
  });

  it("'Refine this' calls refineBacklogItems with exactly [row.id]", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);

    await act(async () => {
      renderTab((p) => p === "queue_job");
    });
    await act(async () => {});

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Refine this" }));
    });
    await act(async () => {});

    expect(vi.mocked(refineBacklogItems)).toHaveBeenCalledWith(1, [ITEM.id]);
  });

  it("multi-select 'Refine selected' calls refineBacklogItems with all selected IDs", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM, ITEM2]);

    await act(async () => {
      renderTab((p) => p === "queue_job");
    });
    await act(async () => {});

    fireEvent.click(screen.getByRole("checkbox", { name: `Select ${ITEM.title}` }));
    fireEvent.click(screen.getByRole("checkbox", { name: `Select ${ITEM2.title}` }));

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: /Refine selected/ }));
    });
    await act(async () => {});

    expect(vi.mocked(refineBacklogItems)).toHaveBeenCalledWith(
      1,
      expect.arrayContaining([ITEM.id, ITEM2.id])
    );
  });

  // --- Mark Completed tests ---

  it("'Mark Completed' button is visible under propose_backlog for a new-status item", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);

    await act(async () => {
      renderTab((p) => p === "propose_backlog");
    });
    await act(async () => {});

    expect(screen.getByRole("button", { name: "Mark Completed" })).toBeInTheDocument();
  });

  it("'Mark Completed' button is absent without propose_backlog", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);

    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    expect(screen.queryByRole("button", { name: "Mark Completed" })).toBeNull();
  });

  it("'Mark Completed' button is absent for items already completed or converted", async () => {
    const completedItem = { ...ITEM, id: 3, status: "completed" };
    const convertedItem = { ...ITEM2, id: 4, status: "converted" };
    vi.mocked(listBacklogItems).mockResolvedValueOnce([completedItem, convertedItem]);

    await act(async () => {
      renderTab((p) => p === "propose_backlog");
    });
    await act(async () => {});

    expect(screen.queryByRole("button", { name: "Mark Completed" })).toBeNull();
  });

  it("clicking 'Mark Completed' calls patchBacklogItem and updates the row's status", async () => {
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM]);
    vi.mocked(patchBacklogItem).mockResolvedValueOnce({ ...ITEM, status: "completed" });

    await act(async () => {
      renderTab((p) => p === "propose_backlog");
    });
    await act(async () => {});

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Mark Completed" }));
    });
    await act(async () => {});

    expect(vi.mocked(patchBacklogItem)).toHaveBeenCalledWith(ITEM.id, { status: "completed" });
    expect(screen.getByText("completed")).toBeInTheDocument();
  });

  // --- Terminal-status gating tests ---

  it.each(["declined", "completed", "converted"])(
    "no Accept/Decline/Refine controls and a disabled checkbox for a %s item",
    async (status) => {
      const terminalItem = { ...ITEM, status };
      vi.mocked(listBacklogItems).mockResolvedValueOnce([terminalItem]);

      await act(async () => {
        renderTab((p) => p === "triage_backlog" || p === "queue_job" || p === "propose_backlog");
      });
      await act(async () => {});

      expect(screen.queryByRole("button", { name: "Accept" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Decline" })).toBeNull();
      expect(screen.queryByRole("button", { name: "Refine this" })).toBeNull();
      expect(screen.getByRole("checkbox", { name: `Select ${ITEM.title}` })).toBeDisabled();
    }
  );

  it.each(["new", "accepted"])(
    "Accept/Decline/Refine controls and an enabled checkbox still show for a %s item",
    async (status) => {
      const item = { ...ITEM, status };
      vi.mocked(listBacklogItems).mockResolvedValueOnce([item]);

      await act(async () => {
        renderTab((p) => p === "triage_backlog" || p === "queue_job" || p === "propose_backlog");
      });
      await act(async () => {});

      expect(screen.getByRole("button", { name: "Accept" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Decline" })).toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Refine this" })).toBeInTheDocument();
      expect(screen.getByRole("checkbox", { name: `Select ${ITEM.title}` })).not.toBeDisabled();
    }
  );

  it("select-all only selects non-terminal items when the filtered list has a mix", async () => {
    const declinedItem = { ...ITEM2, status: "declined" };
    vi.mocked(listBacklogItems).mockResolvedValueOnce([ITEM, declinedItem]);

    await act(async () => {
      renderTab(() => false);
    });
    await act(async () => {});

    const selectAll = screen.getByRole("checkbox", { name: "Select all" });
    const newRowCheckbox = screen.getByRole("checkbox", { name: `Select ${ITEM.title}` });
    const declinedRowCheckbox = screen.getByRole("checkbox", {
      name: `Select ${declinedItem.title}`,
    });

    fireEvent.click(selectAll);

    expect(newRowCheckbox.checked).toBe(true);
    expect(declinedRowCheckbox.checked).toBe(false);
    expect(declinedRowCheckbox).toBeDisabled();
    expect(selectAll.checked).toBe(true);
  });
});
