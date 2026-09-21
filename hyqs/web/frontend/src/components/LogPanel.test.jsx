import { act, fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { streamLogs } from "../api.js";
import { LogPanel } from "./LogPanel.jsx";

vi.mock("../api.js", () => ({
  streamLogs: vi.fn(),
}));

let streams;

beforeEach(() => {
  streams = [];
  streamLogs.mockReset();
  streamLogs.mockImplementation((jobId, afterId, onLines, onError) => {
    const stream = { jobId, afterId, onLines, onError, close: vi.fn() };
    streams.push(stream);
    return stream;
  });
});

describe("LogPanel", () => {
  it("shows a waiting state while an active stream has no rows", () => {
    render(<LogPanel jobId={1} running />);

    expect(screen.getByRole("status")).toHaveTextContent("Waiting for log output");
    expect(streamLogs).toHaveBeenCalledWith(1, 0, expect.any(Function), expect.any(Function));
  });

  it("shows a pre-data error and reconnects once on Retry", () => {
    render(<LogPanel jobId={1} running />);
    act(() => streams[0].onError());

    expect(screen.getByRole("alert")).toHaveTextContent("Couldn’t connect");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    expect(streams[0].close).toHaveBeenCalledTimes(1);
    expect(streams).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("Waiting for log output");
  });

  it("groups consecutive attempts and marks a superseded attempt failed", () => {
    render(<LogPanel jobId={1} running />);
    act(() =>
      streams[0].onLines([
        { stage: "build", attempt: 0, line: "first" },
        { stage: "build", attempt: 0, line: "second" },
        { stage: "test", attempt: 0, line: "testing" },
        { stage: "build", attempt: 1, line: "fixed" },
      ])
    );

    expect(
      screen.getByText((_, element) => element.textContent === "first\nsecond")
    ).toBeInTheDocument();
    expect(screen.getByText("testing")).toBeInTheDocument();
    expect(screen.getByText("fixed")).toBeInTheDocument();
    expect(screen.getAllByText(/Attempt/)).toHaveLength(3);
    expect(screen.getAllByText(/failed/)).toHaveLength(1);
  });

  it("retains populated logs and surfaces a later degraded state", () => {
    render(<LogPanel jobId={1} running />);
    act(() => streams[0].onLines([{ stage: "build", attempt: 0, line: "kept line" }]));
    act(() => streams[0].onError());

    expect(screen.getByText("kept line")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("log stream was interrupted");

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(screen.getByText("kept line")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("auto-scrolls when rows arrive", () => {
    const { container } = render(<LogPanel jobId={1} running />);
    const panel = container.querySelector(".log-panel");
    Object.defineProperty(panel, "scrollHeight", { configurable: true, value: 240 });

    act(() => streams[0].onLines([{ stage: "build", attempt: 0, line: "line" }]));

    expect(panel.scrollTop).toBe(240);
  });

  it("cleans up on job changes and ignores stale callbacks", () => {
    const { rerender } = render(<LogPanel jobId={1} running />);
    act(() => streams[0].onLines([{ stage: "build", attempt: 0, line: "old line" }]));

    rerender(<LogPanel jobId={2} running />);

    expect(streams[0].close).toHaveBeenCalledTimes(1);
    expect(streams).toHaveLength(2);
    expect(screen.queryByText("old line")).not.toBeInTheDocument();
    act(() => streams[0].onLines([{ stage: "build", attempt: 0, line: "stale line" }]));
    act(() => streams[0].onError());
    expect(screen.queryByText("stale line")).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    act(() => streams[1].onLines([{ stage: "build", attempt: 0, line: "new line" }]));
    expect(screen.getByText("new line")).toBeInTheDocument();
  });

  it("closes its stream on unmount", () => {
    const { unmount } = render(<LogPanel jobId={1} running />);
    unmount();
    expect(streams[0].close).toHaveBeenCalledTimes(1);
  });

  it("renders nothing and opens no stream when inactive and empty", () => {
    const { container } = render(<LogPanel jobId={1} running={false} />);
    expect(container).toBeEmptyDOMElement();
    expect(streamLogs).not.toHaveBeenCalled();
  });
});
