import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { JobDetailPage } from "./JobDetailPage.jsx";
import { ToastProvider } from "../components/Toast.jsx";
import { RoleContext } from "../context.js";

vi.mock("../components/JobDetailTabs.jsx", () => ({
  JobDetailTabs: ({ job }) => <div data-testid="job-detail-tabs" data-job-id={job.id} />,
}));

vi.mock("../hooks/useJobDetailLive.js", () => ({
  useJobDetailLive: vi.fn(),
}));

vi.mock("../api.js", () => ({
  listAgents: vi.fn(() => Promise.resolve([])),
  cancelJob: vi.fn(() => Promise.resolve()),
  retryJob: vi.fn(() => Promise.resolve()),
  archiveJob: vi.fn(() => Promise.resolve()),
  unarchiveJob: vi.fn(() => Promise.resolve()),
}));

const project = { id: 1, name: "Demo Project" };
const epics = [{ id: 7, name: "Checkout revamp" }];

function makeJob(overrides) {
  return {
    id: 42,
    project_id: 1,
    epic_id: 7,
    title: "Add coupons",
    idea: "Add coupons to checkout",
    status: "done",
    stage: "done",
    archived: false,
    created_at: new Date().toISOString(),
    current_executor: null,
    ...overrides,
  };
}

function renderPage(props = {}) {
  return render(
    <ToastProvider>
      <RoleContext.Provider
        value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
      >
        <JobDetailPage jobId={42} project={project} epics={epics} onBack={vi.fn()} {...props} />
      </RoleContext.Provider>
    </ToastProvider>
  );
}

describe("JobDetailPage", () => {
  beforeEach(async () => {
    vi.clearAllMocks();
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: makeJob(),
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });
  });

  it("renders a loading state while live job data is pending", async () => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: null,
      events: [],
      loading: true,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    const { container } = renderPage();
    expect(container.querySelector(".spinner")).toBeInTheDocument();
  });

  it("renders an access message when live job access is forbidden", async () => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: null,
      events: [],
      loading: false,
      forbidden: true,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    expect(screen.getByText("Access denied")).toBeInTheDocument();
    expect(screen.getByText("You don't have permission to view this job.")).toBeInTheDocument();
  });

  it("renders a working Retry button when the live job connection fails", async () => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const retry = vi.fn();
    useJobDetailLive.mockReturnValue({
      job: null,
      events: [],
      loading: false,
      forbidden: false,
      error: true,
      retry,
    });

    renderPage();

    expect(screen.getByText("Unable to load job")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(retry).toHaveBeenCalledOnce();
  });

  it("renders a loaded job's header, historical attribution, and JobDetailTabs", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const job = makeJob();
    useJobDetailLive.mockReturnValue({
      job,
      events: [
        {
          stage: "build",
          agent_id: 5,
          started_at: "2026-07-19T10:00:00Z",
          ended_at: "2026-07-19T10:00:30Z",
          detail: {},
        },
      ],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });
    api.listAgents.mockResolvedValue([
      { id: 5, name: "Claude Builder", provider: "claude", model: "sonnet" },
    ]);

    renderPage();

    expect(await screen.findByText("#42")).toBeInTheDocument();
    expect(screen.getByText("Add coupons")).toBeInTheDocument();
    expect(screen.getByText("done")).toBeInTheDocument();
    expect(screen.getByText("Checkout revamp")).toBeInTheDocument();
    expect(screen.getByText("Duration 30s")).toBeInTheDocument();
    expect(screen.getByText("History: Claude Builder · sonnet")).toBeInTheDocument();
    expect(screen.queryByText(/Current executor:/)).not.toBeInTheDocument();
    expect(screen.getByTestId("job-detail-tabs")).toHaveAttribute("data-job-id", "42");
  });

  it("uses the current executor roster agent and updates on live reassignment", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    let snapshot = {
      job: makeJob({
        status: "running",
        agent_id: 5,
        current_executor: {
          kind: "agent",
          label: "claude",
          agent_id: 5,
          provider: "claude",
        },
      }),
      events: [{ stage: "plan", agent_id: 5, detail: {} }],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    };
    useJobDetailLive.mockImplementation(() => snapshot);
    api.listAgents.mockResolvedValue([
      { id: 5, name: "Claude Planner", provider: "claude", model: "sonnet" },
      { id: 8, name: "Codex Builder", provider: "codex", model: "gpt-5.3-codex" },
    ]);

    const view = renderPage();

    expect(
      await screen.findByText("Current executor: Claude Planner · sonnet")
    ).toBeInTheDocument();
    snapshot = {
      ...snapshot,
      job: makeJob({
        status: "running",
        agent_id: 8,
        current_executor: {
          kind: "agent",
          label: "codex",
          agent_id: 8,
          provider: "codex",
        },
      }),
    };
    view.rerender(
      <ToastProvider>
        <RoleContext.Provider
          value={{ role: "project_admin", can: () => true, authLoading: false, authError: false }}
        >
          <JobDetailPage jobId={42} project={project} epics={epics} onBack={vi.fn()} />
        </RoleContext.Provider>
      </ToastProvider>
    );
    expect(
      await screen.findByText("Current executor: Codex Builder · gpt-5.3-codex")
    ).toBeInTheDocument();
    expect(
      screen.queryByText("Current executor: Claude Planner · sonnet")
    ).not.toBeInTheDocument();
    expect(screen.getByText("History: Claude Planner · sonnet")).toBeInTheDocument();
  });

  it.each([
    ["pipeline", "Pipeline"],
    ["waiting", "Waiting assignment"],
  ])("renders the backend-provided %s executor label", async (kind, label) => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: makeJob({
        status: "running",
        current_executor: { kind, label, agent_id: null, provider: "" },
      }),
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    expect(screen.getByText(`Current executor: ${label}`)).toBeInTheDocument();
  });

  it("does not turn stale terminal ownership or event attribution into a current executor", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: makeJob({
        status: "done",
        agent_id: 5,
        provider: "claude",
        current_executor: null,
      }),
      events: [{ stage: "build", agent_id: 5, detail: {} }],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });
    api.listAgents.mockResolvedValue([
      { id: 5, name: "Claude Builder", provider: "claude", model: "sonnet" },
    ]);

    renderPage();

    expect(await screen.findByText("History: Claude Builder · sonnet")).toBeInTheDocument();
    expect(screen.queryByText(/Current executor:/)).not.toBeInTheDocument();
  });

  it("keeps enriched mixed-agent events as historical attribution", async () => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    useJobDetailLive.mockReturnValue({
      job: makeJob({ current_executor: null }),
      events: [
        {
          stage: "plan",
          agent_id: 3,
          agent_name: "Planner",
          agent_provider: "claude",
          agent_model: "claude-sonnet",
          detail: {},
        },
        {
          stage: "lint",
          agent_id: null,
          agent_name: null,
          agent_provider: null,
          agent_model: null,
          detail: {},
        },
        {
          stage: "review",
          agent_id: 9,
          agent_name: "Reviewer",
          agent_provider: "codex",
          agent_model: "gpt-5.3-codex",
          detail: {},
        },
      ],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    expect(
      screen.getByText("History: Planner · claude-sonnet, Reviewer · gpt-5.3-codex")
    ).toBeInTheDocument();
    expect(screen.queryByText(/Current executor:/)).not.toBeInTheDocument();
  });

  it("clicking Cancel calls cancelJob only when the job is eligible", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const job = makeJob({ status: "running" });
    useJobDetailLive.mockReturnValue({
      job,
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    const cancelBtn = await screen.findByRole("button", { name: "Cancel" });
    fireEvent.click(cancelBtn);
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith(42));
    expect(api.retryJob).not.toHaveBeenCalled();
  });

  it("clicking Retry calls retryJob only when the job is eligible", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const job = makeJob({ status: "failed" });
    useJobDetailLive.mockReturnValue({
      job,
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    const retryBtn = await screen.findByRole("button", { name: "Retry" });
    fireEvent.click(retryBtn);
    await waitFor(() => expect(api.retryJob).toHaveBeenCalledWith(42));
    expect(api.cancelJob).not.toHaveBeenCalled();
  });

  it("does not render Cancel/Retry for a done, unarchived job — only Archive is eligible", async () => {
    const api = await import("../api.js");
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const job = makeJob({ status: "done", archived: false });
    useJobDetailLive.mockReturnValue({
      job,
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });

    renderPage();

    await screen.findByRole("button", { name: "Archive" });
    expect(screen.queryByRole("button", { name: "Cancel" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Retry" })).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Archive" }));
    await waitFor(() => expect(api.archiveJob).toHaveBeenCalledWith(42));
  });

  it("clicking the epic breadcrumb calls onBack", async () => {
    const { useJobDetailLive } = await import("../hooks/useJobDetailLive.js");
    const job = makeJob();
    useJobDetailLive.mockReturnValue({
      job,
      events: [],
      loading: false,
      forbidden: false,
      error: false,
      retry: vi.fn(),
    });
    const onBack = vi.fn();

    renderPage({ onBack });

    const crumb = await screen.findByText("Checkout revamp");
    fireEvent.click(crumb);
    expect(onBack).toHaveBeenCalled();
  });
});
