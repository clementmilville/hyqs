import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { AdminProviders } from "./AdminProviders.jsx";
import { listProviders, clearProviderPause } from "../api.js";

vi.mock("../api.js", () => ({
  listProviders: vi.fn(),
  clearProviderPause: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("AdminProviders loading/forbidden/error", () => {
  it("shows a Spinner while the first fetch is pending", () => {
    listProviders.mockReturnValue(new Promise(() => {}));

    render(<AdminProviders />);

    expect(document.querySelector(".spinner")).not.toBeNull();
  });

  it("shows the access-denied message on a forbidden rejection", async () => {
    listProviders.mockRejectedValue(new Error("forbidden"));

    render(<AdminProviders />);

    expect(await screen.findByText(/don.t have access/i)).toBeInTheDocument();
  });

  it("shows the error message + Retry button on a generic rejection, and Retry re-fetches", async () => {
    listProviders.mockRejectedValue(new Error("network down"));

    render(<AdminProviders />);

    const retryBtn = await screen.findByText("Retry");
    expect(retryBtn).toBeInTheDocument();

    listProviders.mockResolvedValue([
      { provider: "anthropic", paused: false, paused_until_iso: null },
    ]);
    fireEvent.click(retryBtn);

    expect(await screen.findByText("anthropic")).toBeInTheDocument();
    expect(listProviders).toHaveBeenCalledTimes(2);
  });
});

describe("AdminProviders card rendering", () => {
  it("renders one card per provider with the correct badge status", async () => {
    listProviders.mockResolvedValue([
      { provider: "anthropic", paused: false, paused_until_iso: null },
      { provider: "openai", paused: true, paused_until_iso: "2026-07-14T00:00:00+00:00" },
    ]);

    render(<AdminProviders />);

    const cards = await screen.findAllByText(/anthropic|openai/);
    expect(cards).toHaveLength(2);
    expect(document.querySelectorAll(".card")).toHaveLength(2);

    const badges = document.querySelectorAll(".badge");
    expect(Array.from(badges).some((b) => b.classList.contains("ok"))).toBe(true);
    expect(Array.from(badges).some((b) => b.classList.contains("bad"))).toBe(true);
  });

  it("clicking Clear on a paused provider calls clearProviderPause", async () => {
    listProviders.mockResolvedValue([
      { provider: "openai", paused: true, paused_until_iso: "2026-07-14T00:00:00+00:00" },
    ]);
    clearProviderPause.mockResolvedValue({});

    render(<AdminProviders />);

    const clearBtn = await screen.findByText("Clear");
    fireEvent.click(clearBtn);

    expect(clearProviderPause).toHaveBeenCalledWith("openai");
  });
});
