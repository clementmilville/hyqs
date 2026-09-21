import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ProjectDetail } from "./ProjectDetail.jsx";

vi.mock("../api.js", () => ({ listJobsFiltered: vi.fn(() => Promise.resolve([])) }));

const workLanesProps = vi.fn();
vi.mock("../components/WorkLanes.jsx", () => ({
  WorkLanes: (props) => {
    workLanesProps(props);
    return <button onClick={props.onNewJob}>+ New Job</button>;
  },
}));
vi.mock("../components/JobChatPanel.jsx", () => ({ JobChatPanel: () => <div>Job chat</div> }));

const project = { id: 1, repo_path: "/repo" };
const job = (id, status, projectId = 1) => ({
  id,
  status,
  project_id: projectId,
  title: `Job ${id}`,
});

function renderDetail(props = {}) {
  return render(
    <ProjectDetail
      project={project}
      epics={[]}
      jobs={[]}
      onChanged={vi.fn()}
      onNavEpic={vi.fn()}
      onOpenJob={vi.fn()}
      {...props}
    />
  );
}

describe("ProjectDetail lifecycle snapshot", () => {
  beforeEach(() => vi.clearAllMocks());

  it("shows loading instead of a false empty Work snapshot until the initial refresh resolves", async () => {
    const { listJobsFiltered } = await import("../api.js");
    let resolveRefresh;
    listJobsFiltered.mockReturnValue(
      new Promise((resolve) => {
        resolveRefresh = resolve;
      })
    );

    renderDetail();

    expect(screen.getByLabelText("Loading")).toBeInTheDocument();
    expect(workLanesProps).not.toHaveBeenCalled();

    resolveRefresh([job(2, "done")]);
    await waitFor(() => expect(workLanesProps).toHaveBeenCalled());
    expect(screen.queryByLabelText("Loading")).not.toBeInTheDocument();
    expect(workLanesProps.mock.calls.at(-1)[0].jobs).toEqual([job(2, "done")]);
  });

  it("stops loading and retains live jobs when the initial refresh fails", async () => {
    const { listJobsFiltered } = await import("../api.js");
    listJobsFiltered.mockRejectedValue(new Error("offline"));

    renderDetail({ jobs: [job(3, "pending")] });

    expect(
      await screen.findByText("Couldn't refresh completed work; live work is still shown.")
    ).toBeInTheDocument();
    expect(screen.queryByLabelText("Loading")).not.toBeInTheDocument();
    expect(workLanesProps.mock.calls.at(-1)[0].jobs).toEqual([job(3, "pending")]);
  });

  it("uses one all-status refresh and deduplicates with live active records", async () => {
    const { listJobsFiltered } = await import("../api.js");
    listJobsFiltered.mockResolvedValue([job(1, "running"), job(2, "done")]);
    renderDetail({ jobs: [job(1, "deploying"), job(3, "pending"), job(9, "running", 2)] });
    await act(async () => {});

    expect(listJobsFiltered).toHaveBeenCalledWith("all", 1);
    expect(new Set(workLanesProps.mock.calls.at(-1)[0].jobs.map((item) => item.id))).toEqual(
      new Set([1, 2, 3])
    );
    expect(workLanesProps.mock.calls.at(-1)[0].jobs.find((item) => item.id === 1).status).toBe(
      "deploying"
    );
  });

  it("lets a refreshed terminal record replace a stale deploying record", async () => {
    const { listJobsFiltered } = await import("../api.js");
    listJobsFiltered.mockResolvedValue([job(4, "done")]);
    renderDetail({ jobs: [job(4, "deploying")] });
    await act(async () => {});

    const matches = workLanesProps.mock.calls.at(-1)[0].jobs.filter((item) => item.id === 4);
    expect(matches).toHaveLength(1);
    expect(matches[0].status).toBe("done");
  });

  it("cleans up refreshes and does not leak the previous project snapshot", async () => {
    vi.useFakeTimers();
    const { listJobsFiltered } = await import("../api.js");
    listJobsFiltered.mockImplementation((_status, projectId) =>
      Promise.resolve([job(projectId, "done", projectId)])
    );
    const view = renderDetail();
    await act(async () => {});
    view.rerender(
      <ProjectDetail
        project={{ id: 2, repo_path: "/other" }}
        epics={[]}
        jobs={[]}
        onChanged={vi.fn()}
        onNavEpic={vi.fn()}
        onOpenJob={vi.fn()}
      />
    );
    await act(async () => {});

    expect(workLanesProps.mock.calls.at(-1)[0].jobs.map((item) => item.id)).toEqual([2]);
    act(() => vi.advanceTimersByTime(5000));
    expect(listJobsFiltered).toHaveBeenLastCalledWith("all", 2);
    view.unmount();
    vi.useRealTimers();
  });

  it("retains new-job drawer and navigation callbacks", async () => {
    const onOpenJob = vi.fn();
    renderDetail({ onOpenJob });
    await act(async () => {});
    expect(workLanesProps.mock.calls.at(-1)[0].onOpenJob).toBe(onOpenJob);
    fireEvent.click(screen.getByText("+ New Job"));
    expect(screen.getByText("Job chat")).toBeInTheDocument();
  });
});
