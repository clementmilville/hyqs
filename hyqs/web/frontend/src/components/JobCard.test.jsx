import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { JobCard } from "./JobCard.jsx";
import { ToastProvider } from "./Toast.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  cancelJob: vi.fn(() => Promise.resolve()),
  retryJob: vi.fn(() => Promise.resolve()),
  archiveJob: vi.fn(() => Promise.resolve()),
  unarchiveJob: vi.fn(() => Promise.resolve()),
  setJobPriority: vi.fn(() => Promise.resolve()),
  getJobDependencies: vi.fn(() => Promise.resolve([])),
  addJobDependency: vi.fn(() => Promise.resolve([])),
  removeJobDependency: vi.fn(() => Promise.resolve([])),
  patchJobTitle: vi.fn(() => Promise.resolve({})),
}));

function makeJob(overrides) {
  return {
    id: 1,
    project_id: 1,
    title: "Job",
    status: "done",
    stage: "done",
    priority: 0,
    source: "mcp",
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function renderCard(job, allJobs = [], onOpenJob) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <JobCard job={job} allJobs={allJobs} onOpenJob={onOpenJob} />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("JobCard source actor display", () => {
  it("shows the compact actor label on the source badge when source_actor is populated", () => {
    const job = makeJob({ source: "mcp", source_actor: "alex.doe@example.com" });
    renderCard(job);

    const badge = screen.getByTitle(/Filed by alex.doe@example.com via mcp at/);
    expect(badge.textContent).toContain("mcp");
    expect(badge.textContent).toContain("alex.doe");
    expect(badge.textContent).not.toContain("@example.com");
  });

  it("renders the badge unchanged with no actor label when source_actor is absent", () => {
    const job = makeJob({ source: "mcp", source_actor: null });
    renderCard(job);

    const badge = screen.getByText("mcp");
    expect(badge.textContent).toBe("mcp");
    expect(badge.querySelector(".source-actor")).not.toBeInTheDocument();
  });
});

describe("JobCard dependency-picker candidate badges", () => {
  it("shows the unverified warn badge for an already-satisfied candidate and a plain badge for a normal done candidate", async () => {
    const job = makeJob({ id: 1, status: "running" });
    const allJobs = [
      job,
      makeJob({
        id: 2,
        title: "Already satisfied job",
        status: "done",
        resolution: "already-satisfied",
      }),
      makeJob({ id: 3, title: "Normal done job", status: "done" }),
    ];
    renderCard(job, allJobs);

    fireEvent.click(screen.getByTitle("Edit dependencies"));

    await waitFor(() => screen.getByText("Already satisfied job"));

    const unverifiedItem = screen.getByText("Already satisfied job").closest(".dep-picker-item");
    const unverifiedBadge = unverifiedItem.querySelector(".badge");
    expect(unverifiedBadge.className).toBe("badge warn");
    expect(unverifiedBadge.textContent).toBe("unverified");

    const normalItem = screen.getByText("Normal done job").closest(".dep-picker-item");
    const normalBadge = normalItem.querySelector(".badge");
    expect(normalBadge.textContent).toBe("done");
    expect(normalBadge.className).not.toContain("warn");
  });
});

describe("JobCard effective priority indicator", () => {
  it("shows an effective-priority badge with a reasons tooltip when boosted above base priority", () => {
    const job = makeJob({
      priority: 0,
      effective_priority: 10,
      priority_reasons: [{ reason: "aging", amount: 10, detail: "aging boost" }],
    });
    renderCard(job);

    const badge = screen.getByTitle("+10 aging boost");
    expect(badge.className).toContain("warn");
    expect(badge.textContent).toContain("10");
  });

  it("renders no indicator when effective_priority equals priority", () => {
    const job = makeJob({ priority: 0, effective_priority: 0, priority_reasons: [] });
    const { container } = renderCard(job);

    expect(container.querySelector(".badge.warn")).not.toBeInTheDocument();
  });

  it("renders no indicator when effective_priority is absent from the payload", () => {
    const job = makeJob({ priority: 0 });
    const { container } = renderCard(job);

    expect(container.querySelector(".badge.warn")).not.toBeInTheDocument();
  });
});

describe("JobCard row click navigation", () => {
  it("calls onOpenJob with the job id when the jobhead is clicked", () => {
    const onOpenJob = vi.fn();
    const job = makeJob({ id: 7 });
    const { container } = renderCard(job, [], onOpenJob);

    fireEvent.click(container.querySelector(".jobhead"));

    expect(onOpenJob).toHaveBeenCalledWith(7);
  });

  it("calls onOpenJob with the job id when the StageFlow area is clicked", () => {
    const onOpenJob = vi.fn();
    const job = makeJob({ id: 8 });
    const { container } = renderCard(job, [], onOpenJob);

    const stageFlowWrap = container.querySelectorAll(".clickable")[1];
    fireEvent.click(stageFlowWrap);

    expect(onOpenJob).toHaveBeenCalledWith(8);
  });

  it("does not call onOpenJob when the priority select is clicked", () => {
    const onOpenJob = vi.fn();
    const job = makeJob({ id: 9 });
    renderCard(job, [], onOpenJob);

    fireEvent.click(screen.getByTitle("Job priority"));

    expect(onOpenJob).not.toHaveBeenCalled();
  });

  it("does not call onOpenJob when the title is clicked", () => {
    const onOpenJob = vi.fn();
    const job = makeJob({ id: 10, title: "Editable title" });
    renderCard(job, [], onOpenJob);

    fireEvent.click(screen.getByTitle("Click to edit title"));

    expect(onOpenJob).not.toHaveBeenCalled();
  });

  it("does not call onOpenJob when the dep-picker button is clicked", () => {
    const onOpenJob = vi.fn();
    const job = makeJob({ id: 11, status: "running" });
    const allJobs = [job, makeJob({ id: 12, title: "Other job" })];
    renderCard(job, allJobs, onOpenJob);

    fireEvent.click(screen.getByTitle("Edit dependencies"));

    expect(onOpenJob).not.toHaveBeenCalled();
  });

  it("does not call onOpenJob when cancel, retry, or archive buttons are clicked", () => {
    const onOpenJob = vi.fn();

    const cancellable = makeJob({ id: 13, status: "running" });
    const { unmount: unmountCancel } = renderCard(cancellable, [], onOpenJob);
    fireEvent.click(screen.getByTitle("Cancel job"));
    unmountCancel();

    const retryable = makeJob({ id: 14, status: "failed" });
    const { unmount: unmountRetry } = renderCard(retryable, [], onOpenJob);
    fireEvent.click(screen.getByTitle("Retry job"));
    unmountRetry();

    const archivable = makeJob({ id: 15, status: "done" });
    const { unmount: unmountArchive } = renderCard(archivable, [], onOpenJob);
    fireEvent.click(screen.getByTitle("Archive job"));
    unmountArchive();

    expect(onOpenJob).not.toHaveBeenCalled();
  });
});
