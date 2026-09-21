import { describe, it, expect, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { useAIStream } from "./useAIStream.js";

// streamFn receives (...args, callbacks) — the fake accepts (callbacks) with no positional args
function makeFakeStream(events) {
  return (cbs) => {
    const { onText, onToolUse, onResult, onError } = cbs;
    for (const ev of events) {
      if (ev.type === "text") onText?.(ev.delta);
      else if (ev.type === "tool_use") onToolUse?.(ev);
      else if (ev.type === "result") onResult?.(ev);
      else if (ev.type === "error") onError?.(new Error(ev.message));
    }
    return { cancel: vi.fn() };
  };
}

describe("useAIStream", () => {
  it("text accumulates character by character via onText events", () => {
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(makeFakeStream([
        { type: "text", delta: "H" },
        { type: "text", delta: "i" },
        { type: "result", text: "Hi" },
      ]));
    });

    expect(result.current.text).toBe("Hi");
  });

  it("toolEvents grows per tool_use event", () => {
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(makeFakeStream([
        { type: "tool_use", tool: "Read", input_summary: "Read foo.py" },
        { type: "tool_use", tool: "Bash", input_summary: "Bash pytest" },
        { type: "result", text: "" },
      ]));
    });

    expect(result.current.toolEvents).toHaveLength(2);
    expect(result.current.toolEvents[0].tool).toBe("Read");
    expect(result.current.toolEvents[1].tool).toBe("Bash");
  });

  it("streaming flips to false on result event", () => {
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(makeFakeStream([
        { type: "text", delta: "ok" },
        { type: "result", text: "ok" },
      ]));
    });

    expect(result.current.streaming).toBe(false);
    expect(result.current.result).toMatchObject({ text: "ok" });
  });

  it("streaming flips to false on error event", () => {
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(makeFakeStream([
        { type: "error", message: "oops" },
      ]));
    });

    expect(result.current.streaming).toBe(false);
    expect(result.current.error?.message).toBe("oops");
  });

  it("stop() calls cancel and sets streaming to false", () => {
    const cancelFn = vi.fn();
    const pendingStream = (cbs) => ({ cancel: cancelFn });
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(pendingStream);
    });
    expect(result.current.streaming).toBe(true);

    act(() => {
      result.current.stop();
    });

    expect(cancelFn).toHaveBeenCalledOnce();
    expect(result.current.streaming).toBe(false);
  });

  it("start() clears previous state before new stream", () => {
    const { result } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(makeFakeStream([
        { type: "text", delta: "first" },
        { type: "result", text: "first" },
      ]));
    });
    expect(result.current.text).toBe("first");

    act(() => {
      result.current.start(makeFakeStream([
        { type: "result", text: "" },
      ]));
    });
    expect(result.current.text).toBe("");
  });

  it("stop() on unmount via cleanup effect", () => {
    const cancelFn = vi.fn();
    const pendingStream = () => ({ cancel: cancelFn });
    const { result, unmount } = renderHook(() => useAIStream());

    act(() => {
      result.current.start(pendingStream);
    });

    unmount();
    expect(cancelFn).toHaveBeenCalledOnce();
  });
});
