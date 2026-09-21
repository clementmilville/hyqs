import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { Sidebar, WS_NAV, ADMIN_NAV } from "./Sidebar.jsx";
import { BottomTabBar } from "./BottomTabBar.jsx";
import { RoleContext } from "../context.js";

function renderSidebar(props = {}, can = (action) => action === "edit_project") {
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
      <Sidebar
        context="workspace"
        wsTab="dashboard"
        adminTab={null}
        projects={[]}
        currentProjectId={null}
        onNavWorkspace={vi.fn()}
        onNavAdmin={vi.fn()}
        onSwitch={vi.fn()}
        onSignOut={vi.fn()}
        collapsed={false}
        onToggle={vi.fn()}
        mobileOpen={false}
        onMobileClose={vi.fn()}
        {...props}
      />
    </RoleContext.Provider>
  );
}

function renderBottomBar(props = {}, can = (action) => action === "edit_project") {
  const callbacks = {
    onNavigate: vi.fn(),
    onNavAdmin: vi.fn(),
    onSwitch: vi.fn(),
    onSignOut: vi.fn(),
  };
  const result = render(
    <BottomTabBar
      context="workspace"
      active="dashboard"
      can={can}
      projects={[
        { id: 1, name: "Alpha" },
        { id: 2, name: "Beta" },
      ]}
      currentProjectId={1}
      {...callbacks}
      {...props}
    />
  );
  return { ...result, ...callbacks };
}

describe("Sidebar", () => {
  it("WS_NAV has exactly 5 entries: dashboard, plan, work, history, analytics", () => {
    expect(WS_NAV.map((n) => n.id)).toEqual(["dashboard", "plan", "work", "history", "analytics"]);
  });

  it("renders all 5 WS_NAV destinations plus the gated Settings item", () => {
    renderSidebar();
    for (const { label } of WS_NAV) {
      expect(screen.getByTitle(label)).toBeInTheDocument();
    }
    expect(screen.getByTitle("Settings")).toBeInTheDocument();
  });

  it("labels the dashboard destination 'Overview'", () => {
    renderSidebar();
    expect(screen.getByTitle("Overview")).toBeInTheDocument();
  });

  it("ADMIN_NAV has exactly 7 ids in order: command-center, projects, new-project, providers, access, usage-cost, visitor-analytics", () => {
    expect(ADMIN_NAV.map((n) => n.id)).toEqual([
      "command-center",
      "projects",
      "new-project",
      "providers",
      "access",
      "usage-cost",
      "visitor-analytics",
    ]);
  });

  it("shows the Access nav item for a role with only manage_roles (no create_project/manage_providers)", () => {
    renderSidebar({}, (action) => action === "manage_roles");
    expect(screen.getByTitle("Access")).toBeInTheDocument();
    expect(screen.getByTitle("Command Center")).toBeInTheDocument();
  });

  it("shows the Admin section and Command Center for a role with only view_fleet", () => {
    renderSidebar({}, (action) => action === "view_fleet");
    expect(screen.getByTitle("Command Center")).toBeInTheDocument();
    expect(screen.queryByTitle("Access")).not.toBeInTheDocument();
  });

  it("hides the Admin section for a role with none of the admin/access permissions", () => {
    renderSidebar({}, () => false);
    expect(screen.queryByTitle("Command Center")).not.toBeInTheDocument();
    expect(screen.queryByTitle("Access")).not.toBeInTheDocument();
  });
});

describe("BottomTabBar mobile More navigation", () => {
  it("preserves all five workspace destinations and their navigation callbacks", () => {
    const { onNavigate } = renderBottomBar();
    for (const { label } of WS_NAV) {
      expect(screen.getByTitle(label)).toBeInTheDocument();
    }

    fireEvent.click(screen.getByTitle("Work"));
    expect(onNavigate).toHaveBeenCalledWith("work");
    expect(screen.getByTitle("Overview")).toHaveAttribute("aria-current", "page");
  });

  it("shows Settings only with edit_project and closes after navigating", () => {
    const { onNavigate } = renderBottomBar();
    const more = screen.getByTitle("More");
    expect(more).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(more);
    expect(more).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByRole("button", { name: "Settings" }));

    expect(onNavigate).toHaveBeenCalledWith("settings");
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("dialog", { name: "More navigation" })).not.toBeInTheDocument();
  });

  it("hides Settings without edit_project", () => {
    renderBottomBar({}, () => false);
    fireEvent.click(screen.getByTitle("More"));
    expect(screen.queryByRole("button", { name: "Settings" })).not.toBeInTheDocument();
  });

  it("makes project switching, theme, and sign out reachable and closes after actions", () => {
    const { onSwitch, onSignOut } = renderBottomBar();
    const more = screen.getByTitle("More");

    fireEvent.click(more);
    fireEvent.change(screen.getByTitle("Switch project"), { target: { value: "2" } });
    expect(onSwitch).toHaveBeenCalledWith(2);
    expect(more).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(more);
    fireEvent.click(screen.getByRole("button", { name: "Light mode" }));
    expect(document.documentElement).toHaveAttribute("data-theme", "light");
    expect(more).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(more);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(onSignOut).toHaveBeenCalledOnce();
    expect(more).toHaveAttribute("aria-expanded", "false");
  });

  it("filters admin destinations with the shared permission helpers", () => {
    renderBottomBar({}, (action) => action === "manage_roles");
    fireEvent.click(screen.getByTitle("More"));

    expect(screen.getByRole("button", { name: "Command Center" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Access" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Projects" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Providers" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Usage & Cost" })).not.toBeInTheDocument();
  });

  it("marks active More and admin destination and supports Escape dismissal", () => {
    renderBottomBar(
      { context: "admin", active: "usage-cost" },
      (action) => action === "view_fleet"
    );
    const more = screen.getByTitle("More");
    expect(more).toHaveAttribute("aria-current", "page");

    fireEvent.click(more);
    expect(screen.getByRole("button", { name: "Usage & Cost" })).toHaveAttribute(
      "aria-current",
      "page"
    );
    fireEvent.keyDown(document, { key: "Escape" });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(more).toHaveFocus();
  });
});
