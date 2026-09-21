import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, waitFor, act } from "@testing-library/react";
import { useFleetData } from "./useFleetData.js";
import { streamWorkers, getWorkers, streamSupervisor, getSupervisor } from "../api.js";

vi.mock("../api.js", () => ({
  streamWorkers: vi.fn(),
  getWorkers: vi.fn(),
  streamSupervisor: vi.fn(),
  getSupervisor: vi.fn(),
}));

function fakeStream() {
  return { close: vi.fn() };
}

beforeEach(() => {
  vi.clearAllMocks();
  streamWorkers.mockReturnValue(fakeStream());
  streamSupervisor.mockReturnValue(fakeStream());
});

describe("useFleetData", () => {
  it("fetches workers and supervisor data together and exposes both", async () => {
    getWorkers.mockResolvedValue({ workers: [], stats: { alive: 2 } });
    getSupervisor.mockResolvedValue({ supervisors: [{ id: "s1", status: "leader" }] });

    const { result } = renderHook(() => useFleetData());

    await waitFor(() => expect(result.current.fleetData).not.toBeNull());
    expect(result.current.fleetData.stats.alive).toBe(2);
    expect(result.current.supData.supervisors).toHaveLength(1);
    expect(result.current.error).toBe(false);
    expect(result.current.forbidden).toBe(false);
  });

  it("surfaces a forbidden error distinctly from a generic connection failure", async () => {
    getWorkers.mockRejectedValue(new Error("forbidden"));
    getSupervisor.mockRejectedValue(new Error("forbidden"));

    const { result } = renderHook(() => useFleetData());

    await waitFor(() => expect(result.current.forbidden).toBe(true));
    expect(result.current.error).toBe(false);
  });

  it("surfaces a generic connection failure as an error state, not an eternal spinner", async () => {
    getWorkers.mockRejectedValue(new Error("network down"));
    getSupervisor.mockRejectedValue(new Error("network down"));

    const { result } = renderHook(() => useFleetData());

    await waitFor(() => expect(result.current.error).toBe(true));
    expect(result.current.forbidden).toBe(false);
  });

  it("retry() closes and reopens both EventSources and re-fetches", async () => {
    getWorkers.mockResolvedValue({ workers: [], stats: {} });
    getSupervisor.mockResolvedValue({ supervisors: [] });

    const { result } = renderHook(() => useFleetData());
    await waitFor(() => expect(result.current.fleetData).not.toBeNull());

    const firstWorkerStream = streamWorkers.mock.results[0].value;
    const firstSupStream = streamSupervisor.mock.results[0].value;

    act(() => {
      result.current.retry();
    });

    expect(firstWorkerStream.close).toHaveBeenCalled();
    expect(firstSupStream.close).toHaveBeenCalled();
    await waitFor(() => expect(getWorkers).toHaveBeenCalledTimes(2));
  });
});
