import { describe, it, expect, vi } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { usePageData } from "./usePageData.js";

describe("usePageData", () => {
  it("fetches and exposes data once resolved", async () => {
    const fetchFn = vi.fn(() => Promise.resolve({ items: [1, 2] }));

    const { result } = renderHook(() => usePageData(fetchFn));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual({ items: [1, 2] });
    expect(result.current.forbidden).toBe(false);
    expect(result.current.error).toBe(false);
  });

  it("surfaces a forbidden error distinctly from a generic connection failure", async () => {
    const fetchFn = vi.fn(() => Promise.reject(new Error("forbidden")));

    const { result } = renderHook(() => usePageData(fetchFn));

    await waitFor(() => expect(result.current.forbidden).toBe(true));
    expect(result.current.error).toBe(false);
    expect(result.current.loading).toBe(false);
  });

  it("surfaces a generic connection failure as explicit error state, not an eternal spinner", async () => {
    const fetchFn = vi.fn(() => Promise.reject(new Error("network down")));

    const { result } = renderHook(() => usePageData(fetchFn));

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.forbidden).toBe(false);
    expect(result.current.loading).toBe(false);
  });

  it("retry() re-invokes the fetch", async () => {
    const fetchFn = vi.fn(() => Promise.resolve({ ok: true }));

    const { result } = renderHook(() => usePageData(fetchFn));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchFn).toHaveBeenCalledTimes(1);

    act(() => {
      result.current.retry();
    });

    await waitFor(() => expect(fetchFn).toHaveBeenCalledTimes(2));
  });
});
