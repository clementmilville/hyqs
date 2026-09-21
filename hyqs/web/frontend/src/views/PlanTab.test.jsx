import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { PlanTab } from "./PlanTab.jsx";

vi.mock("./EpicsIndex.jsx", () => ({
  EpicsIndex: () => <div data-testid="epics-index" />,
}));
vi.mock("./BacklogTab.jsx", () => ({
  BacklogTab: () => <div data-testid="backlog-tab" />,
}));
let receivedEpicDetailProps;
vi.mock("./EpicDetail.jsx", () => ({
  EpicDetail: (props) => {
    receivedEpicDetailProps = props;
    return <div data-testid="epic-detail">{props.epic.name}</div>;
  },
}));
vi.mock("../components/JobChatPanel.jsx", () => ({
  JobChatPanel: () => <div data-testid="job-chat-panel" />,
}));

const project = { id: 1, repo_path: "/repo" };
const epics = [{ id: 5, name: "Checkout revamp" }];

function emptyStream(overrides = {}) {
  return {
    panel: null,
    streaming: false,
    text: "",
    toolEvents: [],
    open: vi.fn(),
    stop: vi.fn(),
    toggleChecked: vi.fn(),
    createSelected: vi.fn(),
    dismiss: vi.fn(),
    ...overrides,
  };
}

function renderPlanTab(props = {}) {
  return render(
    <PlanTab
      project={project}
      planTab="epics"
      epics={epics}
      jobs={[]}
      epicId={null}
      epicTab={null}
      onNavEpic={vi.fn()}
      onNavWorkspace={vi.fn()}
      onChanged={vi.fn()}
      note=""
      setNote={vi.fn()}
      showArchivedEpics={false}
      setShowArchivedEpics={vi.fn()}
      ideate={emptyStream()}
      architect={emptyStream()}
      {...props}
    />
  );
}

describe("PlanTab", () => {
  it("renders the Epics/Backlog sub-tab bar driven by PLAN_TABS, defaulting to Epics", () => {
    renderPlanTab();
    expect(screen.getByText("Epics")).toBeInTheDocument();
    expect(screen.getByText("Backlog")).toBeInTheDocument();
    expect(screen.getByTestId("epics-index")).toBeInTheDocument();
  });

  it("renders BacklogTab when planTab is 'backlog'", () => {
    renderPlanTab({ planTab: "backlog" });
    expect(screen.getByTestId("backlog-tab")).toBeInTheDocument();
  });

  it("navigating sub-tabs calls onNavWorkspace with the plan sub-tab", () => {
    const onNavWorkspace = vi.fn();
    renderPlanTab({ onNavWorkspace });
    fireEvent.click(screen.getByText("Backlog"));
    expect(onNavWorkspace).toHaveBeenCalledWith(project.id, "plan", "backlog");
  });

  it("renders EpicDetail (and hides the sub-tab bar) when epicId is set", () => {
    renderPlanTab({ epicId: 5 });
    expect(screen.getByTestId("epic-detail")).toHaveTextContent("Checkout revamp");
    expect(screen.queryByText("Backlog")).not.toBeInTheDocument();
  });

  it("forwards onOpenJob to EpicDetail", () => {
    const onOpenJob = vi.fn();
    renderPlanTab({ epicId: 5, onOpenJob });
    expect(screen.getByTestId("epic-detail")).toBeInTheDocument();
    expect(receivedEpicDetailProps.onOpenJob).toBe(onOpenJob);
  });
});
