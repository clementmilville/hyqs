import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import { AdminAudit, coalesceAuditBursts } from "./AdminAudit.jsx";

vi.mock("../api.js", () => ({
  getAuditLog: vi.fn(() => Promise.resolve([])),
}));

import { getAuditLog } from "../api.js";

function entry(overrides) {
  return {
    id: 1,
    at: "2026-01-15T10:00:00Z",
    actor: "worker:1",
    table_name: "role_permissions",
    row_pk: "1",
    action: "insert",
    changed: null,
    ...overrides,
  };
}

describe("coalesceAuditBursts", () => {
  it("merges consecutive same-actor+table+action rows within the same UTC minute", () => {
    const entries = Array.from({ length: 16 }, (_, i) =>
      entry({ id: i + 1, row_pk: String(i + 1), at: "2026-01-15T10:00:0" + (i % 10) + "Z" })
    );

    const bursts = coalesceAuditBursts(entries);

    expect(bursts).toHaveLength(1);
    expect(bursts[0].count).toBe(16);
    expect(bursts[0].rowPks).toHaveLength(16);
  });

  it("does not merge rows with a different action", () => {
    const entries = [entry({ id: 1, action: "insert" }), entry({ id: 2, action: "update" })];

    expect(coalesceAuditBursts(entries)).toHaveLength(2);
  });

  it("does not merge rows more than a minute apart", () => {
    const entries = [
      entry({ id: 1, at: "2026-01-15T10:00:00Z" }),
      entry({ id: 2, at: "2026-01-15T10:05:00Z" }),
    ];

    expect(coalesceAuditBursts(entries)).toHaveLength(2);
  });

  it("does not merge rows from a different actor or table", () => {
    const entries = [entry({ id: 1, actor: "worker:1" }), entry({ id: 2, actor: "worker:2" })];

    expect(coalesceAuditBursts(entries)).toHaveLength(2);
  });
});

describe("AdminAudit", () => {
  beforeEach(() => {
    vi.mocked(getAuditLog).mockReset();
    vi.mocked(getAuditLog).mockResolvedValue([]);
  });

  it("renders a forbidden message when the audit log is not accessible", async () => {
    vi.mocked(getAuditLog).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      render(<AdminAudit />);
    });

    expect(await screen.findByText("You don't have access to this data.")).toBeInTheDocument();
  });

  it("renders an error message with a working Retry button on a connection failure", async () => {
    vi.mocked(getAuditLog).mockRejectedValueOnce(new Error("network down"));
    vi.mocked(getAuditLog).mockResolvedValueOnce([]);

    await act(async () => {
      render(<AdminAudit />);
    });

    expect(await screen.findByText("Couldn't connect to the server.")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() =>
      expect(screen.queryByText("Couldn't connect to the server.")).not.toBeInTheDocument()
    );
    expect(screen.getByText("No audit entries match.")).toBeInTheDocument();
  });

  it("groups coalesced bursts by day and renders a xN chip for a burst", async () => {
    const entries = [
      ...Array.from({ length: 16 }, (_, i) =>
        entry({ id: i + 1, row_pk: String(i + 1), at: "2026-01-15T10:00:0" + (i % 10) + "Z" })
      ),
      entry({
        id: 20,
        at: "2026-01-14T09:00:00Z",
        action: "update",
        changed: { name: ["a", "b"] },
      }),
    ];
    vi.mocked(getAuditLog).mockResolvedValueOnce(entries);

    await act(async () => {
      render(<AdminAudit />);
    });

    expect(await screen.findByText("×16")).toBeInTheDocument();
    expect(document.querySelectorAll(".feed-list-day-header")).toHaveLength(2);
  });

  it("collapses the Changed column behind a title + expander", async () => {
    vi.mocked(getAuditLog).mockResolvedValueOnce([
      entry({ id: 1, action: "update", changed: { name: ["old", "new"] } }),
    ]);

    await act(async () => {
      render(<AdminAudit />);
    });

    const expandBtn = await screen.findByText("1 field");
    expect(screen.queryByText(/name: old → new/)).not.toBeInTheDocument();

    fireEvent.click(expandBtn);

    expect(screen.getByText(/name: old → new/)).toBeInTheDocument();
  });
});
