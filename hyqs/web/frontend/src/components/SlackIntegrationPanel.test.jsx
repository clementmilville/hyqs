import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { SlackIntegrationPanel } from "./SlackIntegrationPanel.jsx";
import { RoleContext } from "../context.js";
import {
  listProjectWebhooks,
  createProjectWebhook,
  setWebhookActive,
  deleteWebhook,
  getSlackCredentialStatus,
  setSlackCredential,
} from "../api.js";

vi.mock("../api.js", () => ({
  listProjectWebhooks: vi.fn(),
  createProjectWebhook: vi.fn(),
  setWebhookActive: vi.fn(),
  deleteWebhook: vi.fn(),
  getSlackCredentialStatus: vi.fn(),
  setSlackCredential: vi.fn(),
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
      <SlackIntegrationPanel projectId={1} setNote={vi.fn()} onChanged={vi.fn()} {...props} />
    </RoleContext.Provider>
  );
}

const sampleChannel = {
  id: 5,
  project_id: 1,
  url: "C0123456",
  event_type: "deploy",
  kind: "slack",
  active: true,
  created_by: "user:a@example.com",
  created_at: "2026-07-01T00:00:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
  listProjectWebhooks.mockResolvedValue([]);
  getSlackCredentialStatus.mockResolvedValue({ configured: false, status: "not_configured" });
});

describe("SlackIntegrationPanel", () => {
  it("shows 'Not configured' before a token is set", async () => {
    renderPanel();
    expect(await screen.findByText("Not configured")).toBeInTheDocument();
  });

  it("shows 'Configured ✓' after setSlackCredential succeeds", async () => {
    setSlackCredential.mockResolvedValue({ configured: true, status: "authenticated" });
    getSlackCredentialStatus
      .mockResolvedValueOnce({ configured: false, status: "not_configured" })
      .mockResolvedValueOnce({ configured: true, status: "authenticated" });
    renderPanel();
    await screen.findByText("Not configured");

    fireEvent.change(screen.getByPlaceholderText("Slack bot token (xoxb-...)"), {
      target: { value: "xoxb-secret-token" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    await waitFor(() => expect(setSlackCredential).toHaveBeenCalledWith(1, "xoxb-secret-token"));
    expect(await screen.findByText("Configured ✓")).toBeInTheDocument();
  });

  it("adding a channel calls createProjectWebhook with kind: slack and the entered channel id", async () => {
    createProjectWebhook.mockResolvedValue({ ...sampleChannel });
    renderPanel();
    await waitFor(() => expect(listProjectWebhooks).toHaveBeenCalledWith(1));

    fireEvent.change(screen.getByPlaceholderText("C0123456"), {
      target: { value: "C0123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Add channel" }));

    await waitFor(() =>
      expect(createProjectWebhook).toHaveBeenCalledWith(1, {
        url: "C0123456",
        event_type: "deploy",
        kind: "slack",
      })
    );
  });

  it("toggling a listed channel's active state calls setWebhookActive", async () => {
    listProjectWebhooks.mockResolvedValue([sampleChannel]);
    setWebhookActive.mockResolvedValue({ ...sampleChannel, active: false });
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Disable" }));

    await waitFor(() => expect(setWebhookActive).toHaveBeenCalledWith(5, false));
  });

  it("deletes a channel after confirm", async () => {
    listProjectWebhooks.mockResolvedValue([sampleChannel]);
    deleteWebhook.mockResolvedValue({ deleted: true, id: 5 });
    vi.spyOn(window, "confirm").mockReturnValue(true);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    await waitFor(() => expect(deleteWebhook).toHaveBeenCalledWith(5));
  });

  it("does not delete when the user cancels the confirm", async () => {
    listProjectWebhooks.mockResolvedValue([sampleChannel]);
    vi.spyOn(window, "confirm").mockReturnValue(false);
    renderPanel();

    fireEvent.click(await screen.findByRole("button", { name: "Delete" }));

    expect(deleteWebhook).not.toHaveBeenCalled();
  });

  it("hides Save/Add/toggle/delete controls without manage_webhooks", async () => {
    listProjectWebhooks.mockResolvedValue([sampleChannel]);
    renderPanel({ can: () => false });

    await screen.findByText("C0123456");
    expect(screen.queryByPlaceholderText("Slack bot token (xoxb-...)")).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("C0123456")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Disable" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });
});
