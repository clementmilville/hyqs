import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { WorkspaceSettings } from "./WorkspaceSettings.jsx";
import { RoleContext } from "../context.js";
import {
  deleteProject,
  listAgents,
  listApiTokens,
  listProjectMembers,
  listProjectWebhooks,
  getSlackCredentialStatus,
} from "../api.js";

vi.mock("../api.js", () => ({
  deleteProject: vi.fn(),
  listAgents: vi.fn(),
  listApiTokens: vi.fn(),
  listProjectMembers: vi.fn(),
  listProjectWebhooks: vi.fn(() => Promise.resolve([])),
  getSlackCredentialStatus: vi.fn(() =>
    Promise.resolve({ configured: false, status: "not_configured" })
  ),
}));

const project = { id: 1, name: "Demo Project", max_fix_attempts: 3 };

function renderSettings(props = {}) {
  return render(
    <RoleContext.Provider
      value={{
        role: "project_admin",
        can: () => true,
        authLoading: false,
        authError: false,
        retryAuth: vi.fn(),
      }}
    >
      <WorkspaceSettings
        project={project}
        setNote={vi.fn()}
        onChanged={vi.fn()}
        onDeleted={vi.fn()}
        {...props}
      />
    </RoleContext.Provider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  listAgents.mockResolvedValue([]);
  listApiTokens.mockResolvedValue([]);
  listProjectMembers.mockResolvedValue([]);
  listProjectWebhooks.mockResolvedValue([]);
  getSlackCredentialStatus.mockResolvedValue({ configured: false, status: "not_configured" });
  deleteProject.mockResolvedValue({});
});

describe("WorkspaceSettings danger zone", () => {
  it("renders delete as a small ghost-danger button, not the prominent full-width control", () => {
    renderSettings();
    const btn = screen.getByRole("button", { name: "Delete project" });
    expect(btn.className).toContain("btn-ghost-danger");
    expect(btn.className).not.toContain("btn-danger");
  });

  it("reveals a text input on click; confirm stays disabled until the typed name matches", () => {
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    const input = screen.getByPlaceholderText(project.name);
    expect(input).toBeInTheDocument();

    const confirmBtn = screen.getByRole("button", { name: "Confirm delete" });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: "wrong name" } });
    expect(confirmBtn).toBeDisabled();

    fireEvent.change(input, { target: { value: project.name } });
    expect(confirmBtn).not.toBeDisabled();
  });

  it("calls deleteProject(project.id) once the typed name matches and confirm is clicked", () => {
    renderSettings();
    fireEvent.click(screen.getByRole("button", { name: "Delete project" }));

    const input = screen.getByPlaceholderText(project.name);
    fireEvent.change(input, { target: { value: project.name } });
    fireEvent.click(screen.getByRole("button", { name: "Confirm delete" }));

    expect(deleteProject).toHaveBeenCalledWith(project.id);
  });
});
