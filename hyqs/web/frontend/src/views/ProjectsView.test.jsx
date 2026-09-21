import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { ProjectsView } from "./ProjectsView.jsx";
import { ToastProvider } from "../components/Toast.jsx";

function renderView(props = {}) {
  return render(
    <ToastProvider>
      <ProjectsView
        projects={[]}
        loading={false}
        jobs={[]}
        onOpen={vi.fn()}
        onCreated={vi.fn()}
        onNewProject={vi.fn()}
        {...props}
      />
    </ToastProvider>
  );
}

describe("ProjectsView", () => {
  it("primary New Project button calls onNewProject", () => {
    const onNewProject = vi.fn();
    renderView({ onNewProject });

    screen.getByRole("button", { name: "New Project" }).click();

    expect(onNewProject).toHaveBeenCalledOnce();
  });

  it("does not render the blank-project inline form", () => {
    renderView();

    expect(screen.queryByText(/start blank project/i)).toBeNull();
    expect(screen.queryByPlaceholderText("Project name…")).toBeNull();
  });

  it("renders the Spinner (not the empty state) while loading with no projects yet", () => {
    renderView({ projects: [], loading: true });

    expect(document.querySelector(".spinner")).not.toBeNull();
    expect(screen.queryByText("No projects yet")).toBeNull();
  });

  it("renders EmptyState once loading is done and there are still no projects", () => {
    renderView({ projects: [], loading: false });

    expect(screen.getByText("No projects yet")).toBeInTheDocument();
    expect(document.querySelector(".spinner")).toBeNull();
  });

  it("counts only active jobs belonging to each project", () => {
    renderView({
      projects: [
        {
          id: 1,
          name: "Demo",
          status: "active",
          repo_path: "/tmp/demo",
          job_count: 7,
          last_activity: null,
        },
      ],
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

    expect(screen.getByText(/3 active/)).toBeInTheDocument();
  });
});
