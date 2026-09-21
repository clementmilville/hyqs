import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { fireEvent } from "@testing-library/react";
import { AdminRuntime } from "./AdminRuntime.jsx";
import {
  getProjectsRuntime,
  getProjectRuntime,
  getDeployStatus,
  restartProject,
  stopProject,
  startProject,
  getProjectLogs,
} from "../api.js";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  getProjectsRuntime: vi.fn(),
  getProjectRuntime: vi.fn(),
  getDeployStatus: vi.fn(),
  restartProject: vi.fn(),
  stopProject: vi.fn(),
  startProject: vi.fn(),
  getProjectLogs: vi.fn(),
}));

function renderWithRole(can) {
  return render(
    <RoleContext.Provider value={{ role: "project_admin", can, authLoading: false }}>
      <AdminRuntime />
    </RoleContext.Provider>
  );
}

const DEMO_PROJECT = {
  project_id: 1,
  name: "demo",
  deploy_mode: "single",
  overall: "running",
  health_ok: true,
  drift_mismatch: true,
  configured_port: 8080,
  container_count: 1,
  running_count: 1,
};

const HEALTHY_PROJECT = {
  project_id: 2,
  name: "healthy-project",
  deploy_mode: "single",
  overall: "running",
  health_ok: true,
  drift_mismatch: false,
  configured_port: 9090,
  container_count: 1,
  running_count: 1,
};

const DEMO_DETAIL = {
  deploy_mode: "single",
  containers: [
    {
      container_name: "demo",
      state: "running",
      health: "healthy",
      image_id: "sha256:abcdef1234567890",
      published_ports: [{ host_port: "8080", container_port: "80", protocol: "tcp" }],
    },
  ],
  configured_port: 8080,
  health: { public_url: "https://demo.example.com", http_status: 200, ok: true },
  drift: { mismatch: true, detail: "vhost port 8081 != configured port 8080" },
};

const DRIFT_FIX_COMMAND = "PORT=8081 docker compose up -d proxy";

const DEMO_DETAIL_WITH_FIX = {
  ...DEMO_DETAIL,
  drift: {
    mismatch: true,
    detail: `container is on :8080 but nginx expects :8081; run \`${DRIFT_FIX_COMMAND}\` in /home/demo`,
  },
};

beforeEach(() => {
  vi.clearAllMocks();
  getDeployStatus.mockResolvedValue(null);
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } });
});

describe("AdminRuntime fleet table", () => {
  it("renders rows with a drift warning badge", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });

    render(<AdminRuntime />);

    expect(await screen.findByText("demo")).toBeInTheDocument();
    expect(screen.getByText("⚠ drift")).toBeInTheDocument();
  });

  it("shows an empty state when no projects", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [] });

    render(<AdminRuntime />);

    expect(await screen.findByText("No runtime data")).toBeInTheDocument();
  });

  it("shows a Retry button on connection failure", async () => {
    getProjectsRuntime.mockRejectedValue(new Error("network down"));

    render(<AdminRuntime />);

    expect(await screen.findByText("Retry")).toBeInTheDocument();
  });

  it("shows a no-access message on a forbidden error", async () => {
    getProjectsRuntime.mockRejectedValue(new Error("forbidden"));

    render(<AdminRuntime />);

    expect(await screen.findByText(/don.t have access/i)).toBeInTheDocument();
  });

  it("sorts unhealthy/drifted projects before healthy ones", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [HEALTHY_PROJECT, DEMO_PROJECT] });

    render(<AdminRuntime />);
    await screen.findByText("demo");

    const rows = screen.getAllByRole("row").filter((r) => r.className.includes("runtime-row"));
    expect(rows[0]).toHaveTextContent("demo");
    expect(rows[1]).toHaveTextContent("healthy-project");
  });

  it("renders Port and Containers columns from the fleet roll-up", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });

    render(<AdminRuntime />);
    await screen.findByText("demo");

    expect(screen.getByText("8080")).toBeInTheDocument();
    expect(screen.getByText("1/1")).toBeInTheDocument();
  });
});

describe("AdminRuntime detail panel", () => {
  it("expands a row, fetches getProjectRuntime once, and shows container state/health/ports", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));

    expect(await screen.findByText("healthy")).toBeInTheDocument();
    expect(screen.getByText("8080:80/tcp")).toBeInTheDocument();
    // one "running" badge for the fleet-table overall column, one for the container state
    expect(screen.getAllByText("running")).toHaveLength(2);
    expect(getProjectRuntime).toHaveBeenCalledWith(1);
    expect(getProjectRuntime).toHaveBeenCalledTimes(1);
  });

  it("only calls getProjectRuntime once across multiple expand/collapse toggles", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);

    render(<AdminRuntime />);
    const row = await screen.findByText("demo");

    fireEvent.click(row);
    expect(await screen.findByText("healthy")).toBeInTheDocument();

    fireEvent.click(row);
    expect(screen.queryByText("healthy")).not.toBeInTheDocument();

    fireEvent.click(row);
    expect(await screen.findByText("healthy")).toBeInTheDocument();

    expect(getProjectRuntime).toHaveBeenCalledTimes(1);
  });

  it("surfaces the detail fetch error inline instead of leaving a stuck spinner", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockRejectedValue(new Error("boom"));

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));

    expect(await screen.findByText(/Failed to load: boom/)).toBeInTheDocument();
  });

  it("shows a copyable drift-fix command matching the backend's verbatim string", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL_WITH_FIX);

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));

    const code = await screen.findByText(DRIFT_FIX_COMMAND);
    expect(code.tagName).toBe("CODE");

    fireEvent.click(screen.getByText("Copy"));
    expect(navigator.clipboard.writeText).toHaveBeenCalledWith(DRIFT_FIX_COMMAND);
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("shows 'stale' when the deployed sha is behind main", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    getDeployStatus.mockResolvedValue({
      is_stale: true,
      deployed_sha: "abc123",
      main_tip: "def456",
    });

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));

    expect(await screen.findByText("stale")).toBeInTheDocument();
  });

  it("shows 'up to date' when the deployed sha matches main", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    getDeployStatus.mockResolvedValue({
      is_stale: false,
      deployed_sha: "abc123",
      main_tip: "abc123",
    });

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));

    expect(await screen.findByText("up to date")).toBeInTheDocument();
  });
});

describe("AdminRuntime lifecycle controls", () => {
  it("hides Start/Stop/Restart without queue_job permission", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);

    renderWithRole(() => false);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    expect(screen.queryByText("Restart")).not.toBeInTheDocument();
    expect(screen.queryByText("Stop")).not.toBeInTheDocument();
    expect(screen.queryByText("Start")).not.toBeInTheDocument();
  });

  it("shows Start/Stop/Restart with queue_job permission", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    expect(screen.getByText("Restart")).toBeInTheDocument();
    expect(screen.getByText("Stop")).toBeInTheDocument();
    expect(screen.getByText("Start")).toBeInTheDocument();
  });

  it("clicking Restart calls restartProject directly without a confirm prompt", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    restartProject.mockResolvedValue({ job_id: 55 });
    const confirmSpy = vi.spyOn(window, "confirm");

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    fireEvent.click(screen.getByText("Restart"));

    await waitFor(() => expect(restartProject).toHaveBeenCalledWith(1));
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("clicking Start calls startProject directly without a confirm prompt", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    startProject.mockResolvedValue(DEMO_DETAIL);
    const confirmSpy = vi.spyOn(window, "confirm");

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    fireEvent.click(screen.getByText("Start"));

    await waitFor(() => expect(startProject).toHaveBeenCalledWith(1));
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("clicking Stop prompts for confirmation and skips stopProject when cancelled", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    vi.spyOn(window, "confirm").mockReturnValue(false);

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    fireEvent.click(screen.getByText("Stop"));

    expect(window.confirm).toHaveBeenCalledWith(
      "Stop demo? This will take the app offline."
    );
    expect(stopProject).not.toHaveBeenCalled();
  });

  it("clicking Stop calls stopProject once confirmed", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    stopProject.mockResolvedValue(DEMO_DETAIL);
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    fireEvent.click(screen.getByText("Stop"));

    await waitFor(() => expect(stopProject).toHaveBeenCalledWith(1));
  });

  it("re-fetches the runtime detail after a successful lifecycle action", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    restartProject.mockResolvedValue({ job_id: 55 });

    renderWithRole(() => true);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");
    expect(getProjectRuntime).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("Restart"));

    await waitFor(() => expect(getProjectRuntime).toHaveBeenCalledTimes(2));
    expect(getProjectRuntime).toHaveBeenLastCalledWith(1);
  });
});

describe("AdminRuntime logs drawer", () => {
  it("opens the LogsDrawer for a single-container project when Logs is clicked", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    getProjectLogs.mockResolvedValue({
      service: "demo",
      tail: 200,
      lines: ["hello from demo"],
      truncated: false,
    });

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");

    fireEvent.click(screen.getByText("Logs"));

    expect(await screen.findByText("hello from demo")).toBeInTheDocument();
    expect(getProjectLogs).toHaveBeenCalledWith(1, { service: undefined });
    // single-container project: no service selector
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("shows a service selector for compose projects and passes containers as services", async () => {
    const composeDetail = {
      ...DEMO_DETAIL,
      deploy_mode: "compose",
      containers: [
        { name: "web", state: "running", health: "", published_ports: [] },
        { name: "worker", state: "running", health: "", published_ports: [] },
      ],
    };
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(composeDetail);
    getProjectLogs.mockResolvedValue({
      service: "web",
      tail: 200,
      lines: ["web log"],
      truncated: false,
    });

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("web");

    fireEvent.click(screen.getByText("Logs"));

    await screen.findByText("web log");
    expect(screen.getByRole("combobox")).toBeInTheDocument();
    expect(getProjectLogs).toHaveBeenCalledWith(1, { service: "web" });
  });

  it("closing the drawer hides the log viewer", async () => {
    getProjectsRuntime.mockResolvedValue({ projects: [DEMO_PROJECT] });
    getProjectRuntime.mockResolvedValue(DEMO_DETAIL);
    getProjectLogs.mockResolvedValue({
      service: "demo",
      tail: 200,
      lines: ["hello from demo"],
      truncated: false,
    });

    render(<AdminRuntime />);
    fireEvent.click(await screen.findByText("demo"));
    await screen.findByText("healthy");
    fireEvent.click(screen.getByText("Logs"));
    await screen.findByText("hello from demo");

    fireEvent.click(screen.getByLabelText("Close"));

    expect(screen.queryByText("hello from demo")).not.toBeInTheDocument();
  });
});
