import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { PermissionsMatrix } from "./PermissionsMatrix.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  fetchRolePermissions: vi.fn(() => Promise.resolve({})),
  patchRolePermission: vi.fn(() => Promise.resolve()),
}));

import { fetchRolePermissions, patchRolePermission } from "../api.js";

function renderMatrix() {
  return render(
    <RoleContext.Provider
      value={{
        role: "platform_admin",
        can: () => true,
        setPermissions: vi.fn(),
        authLoading: false,
        authError: false,
        retryAuth: vi.fn(),
      }}
    >
      <PermissionsMatrix />
    </RoleContext.Provider>
  );
}

describe("PermissionsMatrix", () => {
  beforeEach(() => {
    vi.mocked(fetchRolePermissions).mockReset();
    vi.mocked(patchRolePermission).mockReset().mockResolvedValue();
  });

  it("renders a forbidden message when permissions are not accessible", async () => {
    vi.mocked(fetchRolePermissions).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      renderMatrix();
    });

    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
  });

  it("renders an error message with a working Retry button", async () => {
    vi.mocked(fetchRolePermissions).mockRejectedValueOnce(new Error("network down"));
    vi.mocked(fetchRolePermissions).mockResolvedValueOnce({ viewer: [] });

    await act(async () => {
      renderMatrix();
    });

    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    expect(document.querySelector(".sup-table")).toBeInTheDocument();
  });

  it("renders the matrix and toggles a permission on success", async () => {
    vi.mocked(fetchRolePermissions).mockResolvedValueOnce({
      viewer: [],
      contributor: [],
      project_admin: [],
      platform_admin: [],
    });

    await act(async () => {
      renderMatrix();
    });

    const checkboxes = screen.getAllByRole("checkbox");
    const first = checkboxes.find((c) => !c.disabled);
    expect(first.checked).toBe(false);

    await act(async () => {
      fireEvent.click(first);
    });

    expect(patchRolePermission).toHaveBeenCalled();
    expect(first.checked).toBe(true);
  });

  it("includes the automation client role", async () => {
    vi.mocked(fetchRolePermissions).mockResolvedValueOnce({});

    await act(async () => {
      renderMatrix();
    });

    expect(screen.getByText("automation client")).toBeInTheDocument();
  });

  it("reverts the optimistic toggle when the update fails", async () => {
    vi.mocked(fetchRolePermissions).mockResolvedValueOnce({
      viewer: [],
      contributor: [],
      project_admin: [],
      platform_admin: [],
    });
    vi.mocked(patchRolePermission).mockRejectedValueOnce(new Error("nope"));

    await act(async () => {
      renderMatrix();
    });

    const checkboxes = screen.getAllByRole("checkbox");
    const first = checkboxes.find((c) => !c.disabled);

    await act(async () => {
      fireEvent.click(first);
    });

    expect(first.checked).toBe(false);
    expect(screen.getByText(/nope/)).toBeInTheDocument();
  });

  it("uses token-based z-index values (no raw integer literal) for sticky header/column", async () => {
    vi.mocked(fetchRolePermissions).mockResolvedValueOnce({
      viewer: [],
      contributor: [],
      project_admin: [],
      platform_admin: [],
    });

    await act(async () => {
      renderMatrix();
    });

    const headerCell = document.querySelectorAll("thead th")[1];
    const cornerCell = document.querySelector("thead th");
    expect(headerCell.style.zIndex).toBe("var(--z-dropdown)");
    expect(cornerCell.style.zIndex).toBe("calc(var(--z-dropdown) + 1)");
  });
});
