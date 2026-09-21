import { describe, it, expect, vi } from "vitest";
import { render } from "@testing-library/react";
import {
  isUnverifiedDone,
  getStatusBadge,
  getAttentionRank,
  handleJobClick,
  renderCell,
  describePriorityReasons,
} from "./jobTableShared.jsx";

function makeJob(overrides) {
  return { id: 1, status: "done", archived: false, ...overrides };
}

describe("isUnverifiedDone", () => {
  it("is true for a done job resolved as already-satisfied", () => {
    expect(isUnverifiedDone(makeJob({ status: "done", resolution: "already-satisfied" }))).toBe(
      true
    );
  });

  it("is false for a done job with no resolution", () => {
    expect(isUnverifiedDone(makeJob({ status: "done" }))).toBe(false);
  });

  it("is false for a non-done status even with resolution set", () => {
    expect(isUnverifiedDone(makeJob({ status: "failed", resolution: "already-satisfied" }))).toBe(
      false
    );
  });
});

describe("getStatusBadge", () => {
  it("returns the warn-toned badge for an unverified-done job", () => {
    const badge = getStatusBadge(makeJob({ status: "done", resolution: "already-satisfied" }));
    expect(badge.className).toBe("badge warn");
    expect(badge.label).toBe("unverified");
    expect(badge.title).toBeTruthy();
  });

  it("returns the original STATUS_CLASS-based badge for a genuinely done job", () => {
    const badge = getStatusBadge(makeJob({ status: "done" }));
    expect(badge.className).toBe("badge ok");
    expect(badge.label).toBe("done");
  });

  it("returns the original STATUS_CLASS-based badge for other statuses", () => {
    expect(getStatusBadge(makeJob({ status: "failed" }))).toEqual({
      className: "badge bad",
      label: "failed",
      title: undefined,
    });
    expect(getStatusBadge(makeJob({ status: "running" }))).toEqual({
      className: "badge run",
      label: "running",
      title: undefined,
    });
    expect(getStatusBadge(makeJob({ status: "deploying" }))).toEqual({
      className: "badge run",
      label: "deploying",
      title: undefined,
    });
  });
});

describe("getAttentionRank", () => {
  it("orders failed before running/deploying before pending before unverified-done before done before cancelled", () => {
    const failed = getAttentionRank(makeJob({ status: "failed" }));
    const running = getAttentionRank(makeJob({ status: "running" }));
    const deploying = getAttentionRank(makeJob({ status: "deploying" }));
    const pending = getAttentionRank(makeJob({ status: "pending" }));
    const unverifiedDone = getAttentionRank(
      makeJob({ status: "done", resolution: "already-satisfied" })
    );
    const done = getAttentionRank(makeJob({ status: "done" }));
    const cancelled = getAttentionRank(makeJob({ status: "cancelled" }));

    expect(failed).toBeLessThan(running);
    expect(running).toBe(deploying);
    expect(running).toBeLessThan(pending);
    expect(pending).toBeLessThan(unverifiedDone);
    expect(unverifiedDone).toBeLessThan(done);
    expect(done).toBeLessThan(cancelled);
  });

  it("returns 99 for any archived job regardless of resolution", () => {
    expect(getAttentionRank(makeJob({ status: "done", archived: true }))).toBe(99);
    expect(
      getAttentionRank(makeJob({ status: "done", resolution: "already-satisfied", archived: true }))
    ).toBe(99);
    expect(getAttentionRank(makeJob({ status: "failed", archived: true }))).toBe(99);
  });
});

describe("renderCell source", () => {
  it("renders a source-mcp badge with no actor suffix when source_actor is absent", () => {
    const { container } = render(<div>{renderCell(makeJob({ source: "mcp" }), "source")}</div>);
    const badge = container.querySelector(".source-badge");
    expect(badge.className).toContain("source-mcp");
    expect(badge.querySelector(".source-actor")).toBeNull();
  });

  it("renders an actor suffix truncated to the email local-part, with a full tooltip", () => {
    const { container } = render(
      <div>
        {renderCell(makeJob({ source: "ui", source_actor: "person@example.com" }), "source")}
      </div>
    );
    const badge = container.querySelector(".source-badge");
    expect(badge.textContent).toContain("person");
    expect(badge.textContent).not.toContain("person@example.com");
    expect(badge.title).toContain("person@example.com");
    expect(badge.title).toContain("ui");
  });

  it("renders a non-email source_actor verbatim, untruncated", () => {
    const { container } = render(
      <div>{renderCell(makeJob({ source: "cli", source_actor: "nightly-cron" }), "source")}</div>
    );
    const badge = container.querySelector(".source-badge");
    expect(badge.textContent).toContain("nightly-cron");
  });
});

describe("describePriorityReasons", () => {
  it("joins each reason's amount and detail on its own line", () => {
    const summary = describePriorityReasons([
      { reason: "remediation", amount: 3, detail: "remediation boost" },
      { reason: "aging", amount: 1, detail: "aging boost" },
    ]);
    expect(summary).toBe("+3 remediation boost\n+1 aging boost");
  });

  it("falls back to a generic message when reasons is empty or missing", () => {
    expect(describePriorityReasons([])).toBe("Priority boosted");
    expect(describePriorityReasons(undefined)).toBe("Priority boosted");
  });
});

describe("renderCell priority", () => {
  it("renders the base priority with no title when effective_priority matches priority", () => {
    const { container } = render(
      <div>{renderCell(makeJob({ priority: 5, effective_priority: 5 }), "priority")}</div>
    );
    const cell = container.querySelector(".job-table-priority");
    expect(cell.textContent).toBe("5");
    expect(cell.title).toBe("");
  });

  it("renders the base priority with no title when effective_priority is absent", () => {
    const { container } = render(<div>{renderCell(makeJob({ priority: 5 }), "priority")}</div>);
    const cell = container.querySelector(".job-table-priority");
    expect(cell.textContent).toBe("5");
    expect(cell.title).toBe("");
  });

  it("renders the effective priority with a reasons tooltip when boosted above base priority", () => {
    const { container } = render(
      <div>
        {renderCell(
          makeJob({
            priority: 5,
            effective_priority: 8,
            priority_reasons: [{ reason: "aging", amount: 3, detail: "aging boost" }],
          }),
          "priority"
        )}
      </div>
    );
    const cell = container.querySelector(".job-table-priority");
    expect(cell.textContent).toBe("8");
    expect(cell.title).toBe("+3 aging boost");
  });
});

describe("handleJobClick", () => {
  it("calls onOpenJob with the job's id when provided", () => {
    const onOpenJob = vi.fn();
    handleJobClick(makeJob({ id: 42 }), onOpenJob);
    expect(onOpenJob).toHaveBeenCalledWith(42);
  });

  it("does not throw when onOpenJob is undefined", () => {
    expect(() => handleJobClick(makeJob({ id: 42 }), undefined)).not.toThrow();
  });
});
