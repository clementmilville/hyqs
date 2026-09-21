import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { getJob, getJobEvents, streamJobDetail } from "../api.js";
import { useJobDetailLive } from "./useJobDetailLive.js";

vi.mock("../api.js", () => ({
  getJob: vi.fn(),
  getJobEvents: vi.fn(),
  streamJobDetail: vi.fn(),
}));

function fakeStream() {
  return { close: vi.fn() };
}

beforeEach(() => {
  vi.clearAllMocks();
  streamJobDetail.mockReturnValue(fakeStream());
  getJob.mockResolvedValue({ id: 42, status: "queued" });
  getJobEvents.mockResolvedValue([{ id: 1, kind: "created" }]);
});

describe("useJobDetailLive", () => {
  it("populates the job and events from the initial load", async () => {
    const { result } = renderHook(() => useJobDetailLive(42));

    expect(result.current.loading).toBe(true);
    await waitFor(() => expect(result.current.loading).toBe(false));

    expect(result.current.job).toEqual({ id: 42, status: "queued" });
    expect(result.current.events).toEqual([{ id: 1, kind: "created" }]);
    expect(result.current.forbidden).toBe(false);
    expect(result.current.error).toBe(false);
  });

  it("updates the job and events atomically from a stream snapshot", async () => {
    const { result } = renderHook(() => useJobDetailLive(42));
    await waitFor(() => expect(result.current.loading).toBe(false));

    const onData = streamJobDetail.mock.calls[0][1];
    act(() => {
      onData({
        job: { id: 42, status: "building" },
        events: [
          { id: 1, kind: "created" },
          { id: 2, kind: "stage_started" },
        ],
      });
    });

    expect(result.current.job.status).toBe("building");
    expect(result.current.events).toHaveLength(2);
    expect(getJob).toHaveBeenCalledTimes(1);
    expect(getJobEvents).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["forbidden", true, false],
    ["network down", false, true],
  ])("classifies a %s fetch failure", async (message, forbidden, error) => {
    getJob.mockRejectedValue(new Error(message));

    const { result } = renderHook(() => useJobDetailLive(42));

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.forbidden).toBe(forbidden);
    expect(result.current.error).toBe(error);
  });

  it.each([
    ["forbidden", true, false],
    ["connection lost", false, true],
  ])("classifies a %s stream failure", async (message, forbidden, error) => {
    getJob.mockReturnValue(new Promise(() => {}));
    const { result } = renderHook(() => useJobDetailLive(42));
    const onError = streamJobDetail.mock.calls[0][2];

    act(() => {
      onError(new Error(message));
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.forbidden).toBe(forbidden);
    expect(result.current.error).toBe(error);
  });

  it("keeps successfully fetched data when the stream fails during startup", async () => {
    let resolveJob;
    getJob.mockReturnValue(new Promise((resolve) => (resolveJob = resolve)));
    const { result } = renderHook(() => useJobDetailLive(42));
    const onError = streamJobDetail.mock.calls[0][2];

    act(() => onError(new Error("connection lost")));
    await waitFor(() => expect(result.current.error).toBe(true));

    act(() => resolveJob({ id: 42, status: "running" }));
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.job).toEqual({ id: 42, status: "running" });
    expect(result.current.error).toBe(false);
  });

  it("retry closes and reopens the stream and repeats both fetches", async () => {
    const { result } = renderHook(() => useJobDetailLive(42));
    await waitFor(() => expect(result.current.loading).toBe(false));
    const firstStream = streamJobDetail.mock.results[0].value;

    act(() => {
      result.current.retry();
    });

    expect(firstStream.close).toHaveBeenCalledOnce();
    await waitFor(() => expect(streamJobDetail).toHaveBeenCalledTimes(2));
    expect(getJob).toHaveBeenCalledTimes(2);
    expect(getJobEvents).toHaveBeenCalledTimes(2);
  });
});
