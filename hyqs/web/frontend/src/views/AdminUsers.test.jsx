import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { AdminUsers } from "./AdminUsers.jsx";

vi.mock("../api.js", () => ({
  listAdminUsers: vi.fn(() => Promise.resolve([])),
}));

import { listAdminUsers } from "../api.js";

describe("AdminUsers", () => {
  beforeEach(() => {
    vi.mocked(listAdminUsers).mockReset();
  });

  it("renders a forbidden message instead of a silently empty table", async () => {
    vi.mocked(listAdminUsers).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      render(<AdminUsers onOpenUser={vi.fn()} />);
    });

    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
  });

  it("renders an error message with a working Retry button on a connection failure", async () => {
    vi.mocked(listAdminUsers).mockRejectedValueOnce(new Error("network down"));
    vi.mocked(listAdminUsers).mockResolvedValueOnce([
      { id: 1, email: "a@test.com", display_name: "A", is_platform_admin: false },
    ]);

    await act(async () => {
      render(<AdminUsers onOpenUser={vi.fn()} />);
    });

    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    expect(await screen.findByText("a@test.com")).toBeInTheDocument();
  });

  it("renders the users table on success and calls onOpenUser when Manage is clicked", async () => {
    vi.mocked(listAdminUsers).mockResolvedValueOnce([
      { id: 7, email: "user7@test.com", display_name: "User Seven", is_platform_admin: true },
    ]);
    const onOpenUser = vi.fn();

    await act(async () => {
      render(<AdminUsers onOpenUser={onOpenUser} />);
    });

    expect(screen.getByText("user7@test.com")).toBeInTheDocument();
    expect(screen.getByText("platform_admin")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Manage" }));
    expect(onOpenUser).toHaveBeenCalledWith(7);
  });
});
