import { describe, it, expect } from "vitest";
import { dur } from "./utils.js";

describe("dur", () => {
  it("handles offset-qualified timestamps (+00:00)", () => {
    expect(dur(
      "2026-07-02T08:54:00.000000+00:00",
      "2026-07-02T08:56:15.000000+00:00"
    )).toBe("2m 15s");
  });

  it("handles Z-terminated timestamps", () => {
    expect(dur(
      "2026-07-02T08:54:00.000000Z",
      "2026-07-02T08:56:15.000000Z"
    )).toBe("2m 15s");
  });

  it("handles naive timestamps (no timezone)", () => {
    expect(dur(
      "2026-07-02T08:54:00.000000",
      "2026-07-02T08:56:15.000000"
    )).toBe("2m 15s");
  });
});
