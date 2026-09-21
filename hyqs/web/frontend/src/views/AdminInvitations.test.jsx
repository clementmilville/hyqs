import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, fireEvent } from "@testing-library/react";
import { AdminInvitations } from "./AdminInvitations.jsx";

vi.mock("../api.js", () => ({
  listInvitations: vi.fn(() => Promise.resolve([])),
  createInvitation: vi.fn(() => Promise.resolve({})),
  revokeInvitation: vi.fn(() => Promise.resolve()),
}));

import { listInvitations, revokeInvitation } from "../api.js";

describe("AdminInvitations", () => {
  let confirmSpy;

  beforeEach(() => {
    vi.mocked(listInvitations).mockReset().mockResolvedValue([]);
    vi.mocked(revokeInvitation).mockReset().mockResolvedValue();
    confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);
  });

  afterEach(() => {
    confirmSpy.mockRestore();
  });

  it("renders a forbidden message when invitations are not accessible", async () => {
    vi.mocked(listInvitations).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      render(<AdminInvitations />);
    });

    expect(screen.getByText("You don't have access to this data.")).toBeInTheDocument();
  });

  it("renders an error message with a working Retry button", async () => {
    vi.mocked(listInvitations).mockRejectedValueOnce(new Error("network down"));

    await act(async () => {
      render(<AdminInvitations />);
    });

    expect(screen.getByText("Couldn't connect to the server.")).toBeInTheDocument();

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    });

    expect(screen.getByText("No invitations yet.")).toBeInTheDocument();
  });

  it("renders the invitations table on success", async () => {
    vi.mocked(listInvitations).mockResolvedValueOnce([
      {
        id: 1,
        email: "a@test.com",
        invited_by: "b@test.com",
        expires_at: null,
        consumed_at: null,
        token: "tok1",
      },
    ]);

    await act(async () => {
      render(<AdminInvitations />);
    });

    expect(screen.getByText("a@test.com")).toBeInTheDocument();
  });

  it("confirms before revoking an invitation", async () => {
    vi.mocked(listInvitations).mockResolvedValue([
      {
        id: 1,
        email: "a@test.com",
        invited_by: "",
        expires_at: null,
        consumed_at: null,
        token: "tok1",
      },
    ]);

    await act(async () => {
      render(<AdminInvitations />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    });

    expect(confirmSpy).toHaveBeenCalled();
    expect(revokeInvitation).toHaveBeenCalledWith("tok1");
  });

  it("does not revoke when the confirm is dismissed", async () => {
    confirmSpy.mockReturnValue(false);
    vi.mocked(listInvitations).mockResolvedValue([
      {
        id: 1,
        email: "a@test.com",
        invited_by: "",
        expires_at: null,
        consumed_at: null,
        token: "tok1",
      },
    ]);

    await act(async () => {
      render(<AdminInvitations />);
    });

    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    });

    expect(revokeInvitation).not.toHaveBeenCalled();
  });
});
