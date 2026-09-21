import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { OAuthGate } from "./OAuthGate.jsx";

vi.mock("../api.js", () => ({
  verifyToken: vi.fn(() => Promise.resolve(false)),
  setToken: vi.fn(),
}));

import { verifyToken } from "../api.js";

beforeEach(() => {
  vi.clearAllMocks();
  global.fetch = vi.fn(() =>
    Promise.resolve({ json: () => Promise.resolve({ google: false, apple: false }) })
  );
});

describe("OAuthGate", () => {
  it("renders h1 'Hyqs'", () => {
    render(<OAuthGate onSave={vi.fn()} />);
    expect(screen.getByRole("heading", { name: "Hyqs" })).toBeInTheDocument();
  });

  it("does not show password input on initial render", () => {
    render(<OAuthGate onSave={vi.fn()} />);
    expect(screen.queryByPlaceholderText("HYQS_WEB_TOKEN")).not.toBeInTheDocument();
  });

  it("clicking 'Admin access' button reveals a password input", () => {
    render(<OAuthGate onSave={vi.fn()} />);
    fireEvent.click(screen.getByText("Admin access"));
    expect(screen.getByPlaceholderText("HYQS_WEB_TOKEN")).toBeInTheDocument();
  });

  it("shows Google sign-in link when google provider is configured", async () => {
    global.fetch = vi.fn(() =>
      Promise.resolve({ json: () => Promise.resolve({ google: true, apple: false }) })
    );
    render(<OAuthGate onSave={vi.fn()} />);
    await waitFor(() => {
      expect(screen.getByText("Sign in with Google")).toBeInTheDocument();
    });
  });

  it("shows error text 'Invalid token' when verifyToken rejects", async () => {
    verifyToken.mockResolvedValue(false);
    render(<OAuthGate onSave={vi.fn()} />);
    fireEvent.click(screen.getByText("Admin access"));
    const input = screen.getByPlaceholderText("HYQS_WEB_TOKEN");
    fireEvent.change(input, { target: { value: "bad-token" } });
    fireEvent.submit(input.closest("form"));
    await waitFor(() => {
      expect(screen.getByText(/Invalid token/)).toBeInTheDocument();
    });
  });

  it("never calls verifyToken on mount with a plain URL (no ?token= parsing)", async () => {
    render(<OAuthGate onSave={vi.fn()} />);
    await waitFor(() => {
      expect(global.fetch).toHaveBeenCalledWith("/api/auth/providers");
    });
    expect(verifyToken).not.toHaveBeenCalled();
  });
});
