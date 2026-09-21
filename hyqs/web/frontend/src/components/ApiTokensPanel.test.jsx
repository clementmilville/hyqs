import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ApiTokensPanel } from "./ApiTokensPanel.jsx";
import { RoleContext } from "../context.js";
import { listApiTokens, createApiToken, revokeApiToken } from "../api.js";

vi.mock("../api.js", () => ({
  listApiTokens: vi.fn(),
  createApiToken: vi.fn(),
  revokeApiToken: vi.fn(),
}));

function renderPanel({ can = () => true, ...props } = {}) {
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
      <ApiTokensPanel projectId={1} setNote={vi.fn()} onChanged={vi.fn()} {...props} />
    </RoleContext.Provider>
  );
}

const sampleToken = {
  id: 1,
  project_id: 1,
  name: "CI token",
  role: "contributor",
  last4: "ab12",
  created_by: "user:a@example.com",
  created_at: "2026-07-01T00:00:00Z",
  last_used_at: null,
  revoked_at: null,
};

beforeEach(() => {
  vi.clearAllMocks();
  listApiTokens.mockResolvedValue([]);
});

describe("ApiTokensPanel", () => {
  it("shows an empty hint when there are no tokens", async () => {
    renderPanel();
    await waitFor(() => expect(listApiTokens).toHaveBeenCalledWith(1));
    expect(screen.getByText("No API tokens yet.")).toBeInTheDocument();
  });

  it("lists tokens masked (name, role, last4) without ever showing a secret", async () => {
    listApiTokens.mockResolvedValue([sampleToken]);
    renderPanel();

    expect(await screen.findByText("CI token")).toBeInTheDocument();
    expect(document.querySelector(".badge").textContent).toBe("contributor");
    expect(screen.getByText("•••• ab12")).toBeInTheDocument();
    expect(screen.queryByText(/hpat_/)).not.toBeInTheDocument();
  });

  it("creates a token and reveals the plaintext secret exactly once", async () => {
    const created = { ...sampleToken, token: "hpat_supersecretvalue" };
    createApiToken.mockResolvedValue(created);
    renderPanel();
    await waitFor(() => expect(listApiTokens).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("Token name"), {
      target: { value: "CI token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));

    await waitFor(() => expect(createApiToken).toHaveBeenCalledWith(1, "CI token", "viewer"));
    expect(await screen.findByText("hpat_supersecretvalue")).toBeInTheDocument();
    expect(
      screen.getByText("Copy this secret now — it will not be shown again.")
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText("hpat_supersecretvalue")).not.toBeInTheDocument();
  });

  it("creates unattended tokens with the automation client role", async () => {
    createApiToken.mockResolvedValue({
      ...sampleToken,
      role: "automation_client",
      token: "hpat_automationsecret",
    });
    renderPanel();
    await waitFor(() => expect(listApiTokens).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("Token name"), {
      target: { value: "Orchestrator" },
    });
    fireEvent.change(screen.getByRole("combobox"), {
      target: { value: "automation_client" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));

    await waitFor(() =>
      expect(createApiToken).toHaveBeenCalledWith(1, "Orchestrator", "automation_client")
    );
  });

  it("copies the revealed secret to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    createApiToken.mockResolvedValue({ ...sampleToken, token: "hpat_supersecretvalue" });
    renderPanel();
    await waitFor(() => expect(listApiTokens).toHaveBeenCalled());

    fireEvent.change(screen.getByPlaceholderText("Token name"), {
      target: { value: "CI token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create token" }));
    await screen.findByText("hpat_supersecretvalue");

    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("hpat_supersecretvalue"));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("revokes a token after confirm", async () => {
    listApiTokens.mockResolvedValue([sampleToken]);
    revokeApiToken.mockResolvedValue({ ok: true });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));

    await waitFor(() => expect(revokeApiToken).toHaveBeenCalledWith(1, sampleToken.id));
  });

  it("does not revoke when the user cancels the confirm", async () => {
    listApiTokens.mockResolvedValue([sampleToken]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Revoke" }));

    expect(revokeApiToken).not.toHaveBeenCalled();
  });

  it("hides the create form and revoke button without manage_api_tokens", async () => {
    listApiTokens.mockResolvedValue([sampleToken]);
    renderPanel({ can: () => false });

    await screen.findByText("CI token");
    expect(screen.queryByPlaceholderText("Token name")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Revoke" })).not.toBeInTheDocument();
  });
});
