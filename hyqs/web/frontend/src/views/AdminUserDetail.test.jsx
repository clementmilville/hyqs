import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { AdminUserDetail } from "./AdminUserDetail.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => ({
  listAdminUsers: vi.fn(() => Promise.resolve([])),
  listUserMemberships: vi.fn(() => Promise.resolve([])),
  deleteAdminUser: vi.fn(() => Promise.resolve()),
  assignUserToProject: vi.fn(() => Promise.resolve({})),
  updateMemberRole: vi.fn(() => Promise.resolve({})),
  removeProjectMember: vi.fn(() => Promise.resolve()),
  grantPlatformPermission: vi.fn(() => Promise.resolve()),
  revokePlatformPermission: vi.fn(() => Promise.resolve()),
  listProjects: vi.fn(() => Promise.resolve([])),
  getMe: vi.fn(() => Promise.resolve({ id: 1 })),
}));

import { listAdminUsers, listUserMemberships, removeProjectMember, getMe } from "../api.js";

const USER = {
  id: 7,
  email: "user7@test.com",
  display_name: "User Seven",
  is_platform_admin: false,
  is_active: true,
  platform_permissions: [],
};

function renderDetail(props = {}) {
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
      <AdminUserDetail userId={7} onBack={vi.fn()} {...props} />
    </RoleContext.Provider>
  );
}

describe("AdminUserDetail", () => {
  let confirmSpy;

  beforeEach(() => {
    vi.mocked(listAdminUsers).mockReset().mockResolvedValue([USER]);
    vi.mocked(listUserMemberships).mockReset().mockResolvedValue([]);
    vi.mocked(getMe).mockReset().mockResolvedValue({ id: 1 });
    vi.mocked(removeProjectMember).mockClear();
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it("shows the Spinner while loading", async () => {
    let resolveUsers;
    vi.mocked(listAdminUsers).mockReturnValue(
      new Promise((res) => {
        resolveUsers = res;
      })
    );

    renderDetail();

    expect(document.querySelector(".spinner-wrap")).toBeInTheDocument();

    await act(async () => {
      resolveUsers([USER]);
    });
  });

  it("renders a forbidden message when the user fetch is forbidden", async () => {
    vi.mocked(listAdminUsers).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      renderDetail();
    });

    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
  });

  it("renders an error message with Retry on a connection failure", async () => {
    vi.mocked(listAdminUsers).mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      renderDetail();
    });

    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    expect(await screen.findByText("user7@test.com")).toBeInTheDocument();
  });

  it("renders the user profile on success", async () => {
    await act(async () => {
      renderDetail();
    });

    expect(screen.getByText("user7@test.com")).toBeInTheDocument();
  });

  it("confirms before removing a project membership", async () => {
    vi.mocked(listUserMemberships).mockResolvedValue([
      { project_id: 1, project_name: "Demo", role: "viewer" },
    ]);

    await act(async () => {
      renderDetail();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(removeProjectMember).toHaveBeenCalledWith(1, 7);
  });

  it("does not remove the membership if the confirm is dismissed", async () => {
    confirmSpy.mockReturnValue(false);
    vi.mocked(listUserMemberships).mockResolvedValue([
      { project_id: 1, project_name: "Demo", role: "viewer" },
    ]);

    await act(async () => {
      renderDetail();
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Remove" }));
    });

    expect(removeProjectMember).not.toHaveBeenCalled();
  });
});
