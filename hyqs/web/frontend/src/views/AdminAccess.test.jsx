import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { AdminAccess } from "./AdminAccess.jsx";
import { RoleContext } from "../context.js";

vi.mock("../api.js", () => {
  const res = (v = null) => vi.fn(() => Promise.resolve(v));
  return {
    fetchRolePermissions: res({}),
    patchRolePermission: res(),
    listInvitations: res([]),
    createInvitation: res({}),
    revokeInvitation: res(),
    getAuditLog: res({ entries: [] }),
    listAdminUsers: res([]),
    listUserMemberships: res([]),
    deleteAdminUser: res(),
    assignUserToProject: res({}),
    updateMemberRole: res({}),
    removeProjectMember: res(),
    grantPlatformPermission: res(),
    revokePlatformPermission: res(),
    listProjects: res([]),
    getMe: res({ id: 1 }),
  };
});

function renderAccess(props = {}, can = () => true) {
  return render(
    <RoleContext.Provider
      value={{
        role: "platform_admin",
        can,
        setPermissions: vi.fn(),
        authLoading: false,
        authError: false,
        retryAuth: vi.fn(),
      }}
    >
      <AdminAccess
        subTab={null}
        adminUserId={null}
        onNavTab={vi.fn()}
        onNavAdminUser={vi.fn()}
        {...props}
      />
    </RoleContext.Provider>
  );
}

describe("AdminAccess sub-tabs", () => {
  it("defaults to the 'roles' sub-tab", () => {
    renderAccess();
    const rolesBtn = screen.getByRole("button", { name: "Roles" });
    expect(rolesBtn.className).toContain("active");
    expect(screen.queryByText("You don't have access to this data.")).not.toBeInTheDocument();
  });

  it("a role with only manage_users sees Users content but the shared forbidden message on Roles/Invitations/Audit", async () => {
    const can = (action) => action === "manage_users";

    const { unmount: unmount1 } = renderAccess({ subTab: "roles" }, can);
    expect(await screen.findByText("You don't have access to this data.")).toBeInTheDocument();
    unmount1();

    const { unmount: unmount2 } = renderAccess({ subTab: "invitations" }, can);
    expect(await screen.findByText("You don't have access to this data.")).toBeInTheDocument();
    unmount2();

    const { unmount: unmount3 } = renderAccess({ subTab: "audit" }, can);
    expect(await screen.findByText("You don't have access to this data.")).toBeInTheDocument();
    unmount3();

    renderAccess({ subTab: "users" }, can);
    expect(screen.queryByText("You don't have access to this data.")).not.toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Users" })).toBeInTheDocument();
  });

  it("#admin/access/users/7 renders AdminUserDetail for user 7", async () => {
    const { listAdminUsers } = await import("../api.js");
    listAdminUsers.mockResolvedValue([
      { id: 7, email: "user7@test.com", display_name: "", is_platform_admin: false },
    ]);

    renderAccess({ subTab: "users", adminUserId: 7 });

    expect(await screen.findByText("user7@test.com")).toBeInTheDocument();
    expect(screen.getByText("← Back to users")).toBeInTheDocument();
  });
});
