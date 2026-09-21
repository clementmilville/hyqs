import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { WorkspaceDashboard } from "./WorkspaceDashboard.jsx";
import { RoleContext } from "../context.js";
import {
  listJobsFiltered,
  retryJob,
  listAgents,
  listEpics,
  getDeployStatus,
  triggerDeploy,
} from "../api.js";

vi.mock("../components/JobChatPanel.jsx", () => ({
  JobChatPanel: () => <div data-testid="job-chat-panel" />,
}));

vi.mock("../api.js", () => ({
  listJobsFiltered: vi.fn(() => Promise.resolve([])),
  retryJob: vi.fn(() => Promise.resolve({})),
  listAgents: vi.fn(() => Promise.resolve([])),
  listEpics: vi.fn(() => Promise.resolve([])),
  getDeployStatus: vi.fn(() =>
    Promise.resolve({
      deployed_sha: null,
      deployed_at: null,
      main_tip: "",
      is_stale: false,
      deploy_in_flight: false,
      deploy_job_id: null,
    })
  ),
  triggerDeploy: vi.fn(() => Promise.resolve({ job_id: 42 })),
}));

const PROJECT = { id: 1, name: "Demo", status: "active", description: "", repo_path: "/tmp/demo" };

function renderDashboard(props = {}, { can = () => true } = {}) {
  return render(
    <RoleContext.Provider
      value={{
        role: "project_admin",
        can,
        authLoading: false,
        authError: false,
        retryAuth: vi.fn(),
      }}
    >
      <WorkspaceDashboard project={PROJECT} jobs={[]} note={null} {...props} />
    </RoleContext.Provider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listJobsFiltered.mockResolvedValue([]);
  listAgents.mockResolvedValue([]);
  listEpics.mockResolvedValue([]);
  retryJob.mockResolvedValue({});
  triggerDeploy.mockResolvedValue({ job_id: 42 });
  getDeployStatus.mockResolvedValue({
    deployed_sha: null,
    deployed_at: null,
    main_tip: "",
    is_stale: false,
    deploy_in_flight: false,
    deploy_job_id: null,
  });
});

describe("WorkspaceDashboard KPI row", () => {
  it("renders only the selected project's active job count inline with its label", async () => {
    renderDashboard({
      jobs: [
        { id: 1, project_id: 1, status: "running" },
        { id: 2, project_id: 1, status: "pending" },
        { id: 3, project_id: 1, status: "deploying" },
        { id: 4, project_id: 1, status: "done" },
        { id: 5, project_id: 1, status: "failed" },
        { id: 6, project_id: 1, status: "cancelled" },
        { id: 7, project_id: 2, status: "running" },
      ],
    });
    await waitFor(() => expect(screen.getByText("3")).toBeInTheDocument());
    const val = screen.getByText("3");
    expect(val.className).toBe("dash-stat-val");
    expect(val.style.display).toBe("inline");
    expect(screen.getByText("running")).toBeInTheDocument();
  });

  it("shows the Live/Behind/Never deploy badge in the KPI row", async () => {
    renderDashboard();
    await waitFor(() => expect(screen.getAllByText("Never deployed").length).toBe(2));
  });

  it("shows a Live badge with last-deploy text when a deploy is recorded", async () => {
    getDeployStatus.mockResolvedValue({
      deployed_sha: "abc1234",
      deployed_at: new Date(Date.now() - 60000).toISOString(),
      main_tip: "abc1234",
      is_stale: false,
      deploy_in_flight: false,
      deploy_job_id: null,
    });
    renderDashboard();
    await waitFor(() => expect(screen.getByText("Live")).toBeInTheDocument());
    expect(screen.getByText(/Last deploy:/)).toBeInTheDocument();
  });

  it("truncates the last-done line and exposes the full text via title", async () => {
    listJobsFiltered.mockImplementation((status) =>
      status === "done"
        ? Promise.resolve([
            { id: 7, title: "A very long completed job title", idea: "idea", updated_at: null },
          ])
        : Promise.resolve([])
    );
    renderDashboard();
    await waitFor(() =>
      expect(screen.getByText(/A very long completed job title/)).toBeInTheDocument()
    );
    const lastDone = document.querySelector(".dash-last");
    expect(lastDone.style.textOverflow).toBe("ellipsis");
    expect(lastDone.style.whiteSpace).toBe("nowrap");
    expect(lastDone.getAttribute("title")).toContain("A very long completed job title");
  });
});

describe("WorkspaceDashboard Deploy now button", () => {
  it("shows the idle tooltip when not deploying and nothing is in flight", async () => {
    renderDashboard();
    await waitFor(() =>
      expect(screen.getByText("Deploy now").getAttribute("title")).toBe("Deploy latest main now")
    );
  });

  it("shows a Deploying… tooltip while the local click-triggered request is pending", async () => {
    let resolveDeploy;
    triggerDeploy.mockReturnValue(
      new Promise((res) => {
        resolveDeploy = res;
      })
    );
    renderDashboard();
    await waitFor(() => expect(screen.getByText("Deploy now")).not.toBeDisabled());
    fireEvent.click(screen.getByText("Deploy now"));
    await waitFor(() => expect(screen.getByTitle("Deploying…")).toBeInTheDocument());
    resolveDeploy({ job_id: 99 });
  });

  it("shows an in-flight tooltip when the server reports an active deploy job", async () => {
    getDeployStatus.mockResolvedValue({
      deployed_sha: "abc1234",
      deployed_at: null,
      main_tip: "abc1234",
      is_stale: false,
      deploy_in_flight: true,
      deploy_job_id: 55,
    });
    renderDashboard();
    await waitFor(() => expect(screen.getByTitle("Deploy #55 is in flight")).toBeInTheDocument());
  });
});

describe("WorkspaceDashboard three-state data loading", () => {
  it("shows a loading spinner before the combined fetch resolves", () => {
    listJobsFiltered.mockReturnValue(new Promise(() => {}));
    renderDashboard();
    expect(document.querySelector(".spinner")).toBeTruthy();
  });

  it("shows a forbidden message when the combined fetch is forbidden", async () => {
    listJobsFiltered.mockRejectedValue(new Error("forbidden"));
    renderDashboard();
    await waitFor(() => expect(screen.getByText(/don.t have access/i)).toBeInTheDocument());
  });

  it("shows an error + retry button on a generic failure, and refetches on retry", async () => {
    listJobsFiltered.mockRejectedValueOnce(new Error("network down"));
    listJobsFiltered.mockRejectedValueOnce(new Error("network down"));
    listJobsFiltered.mockResolvedValue([]);
    renderDashboard();
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(screen.getByTestId("job-chat-panel")).toBeInTheDocument());
  });

  it("renders the project header immediately without waiting on the data fetches", () => {
    listJobsFiltered.mockReturnValue(new Promise(() => {}));
    renderDashboard();
    expect(screen.getByText("Demo")).toBeInTheDocument();
  });
});

describe("WorkspaceDashboard needs-attention and retry", () => {
  it("lists failed jobs and retries them", async () => {
    listJobsFiltered.mockImplementation((status) =>
      status === "failed"
        ? Promise.resolve([{ id: 5, title: "broken job", status: "failed", error: "boom" }])
        : Promise.resolve([])
    );
    renderDashboard();
    await waitFor(() => expect(screen.getByText("broken job")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(retryJob).toHaveBeenCalledWith(5));
  });
});

describe("WorkspaceDashboard config health", () => {
  it("hides config health when the caller lacks edit_project", async () => {
    renderDashboard({}, { can: (p) => p === "queue_job" });
    await waitFor(() => expect(screen.getByTestId("job-chat-panel")).toBeInTheDocument());
    expect(screen.queryByText("Config health")).not.toBeInTheDocument();
  });

  it("shows agent count from the config health chip", async () => {
    listAgents.mockResolvedValue([{ id: 1 }, { id: 2 }]);
    renderDashboard();
    await waitFor(() => expect(screen.getByText(/2 agents configured/)).toBeInTheDocument());
  });
});
