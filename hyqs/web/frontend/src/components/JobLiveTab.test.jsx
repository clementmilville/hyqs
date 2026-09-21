import { beforeEach, describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { streamLogs } from "../api.js";
import { JobLiveTab } from "./JobLiveTab.jsx";

vi.mock("../api.js", () => ({
  streamLogs: vi.fn(() => ({ close: vi.fn() })),
}));

const JOB_BASE = {
  id: 1,
  idea: "test",
  status: "done",
  stage: null,
  started_at: null,
  ended_at: null,
  error: null,
  archived: false,
};

beforeEach(() => {
  streamLogs.mockClear();
});

describe("JobLiveTab", () => {
  it.each(["running", "pending", "deploying"])(
    "renders LogPanel and opens a log stream when job is %s",
    (status) => {
      const { container } = render(<JobLiveTab job={{ ...JOB_BASE, status }} />);
      expect(container.querySelector(".job-live-idle")).not.toBeInTheDocument();
      expect(screen.getByRole("status")).toHaveTextContent("Waiting for log output");
      expect(streamLogs).toHaveBeenCalledWith(1, 0, expect.any(Function), expect.any(Function));
    }
  );

  it.each(["done", "failed", "cancelled"])(
    "renders an idle badge and opens no log stream when job is %s",
    (status) => {
      render(<JobLiveTab job={{ ...JOB_BASE, status }} />);
      expect(screen.getByText(/not currently running/i)).toBeInTheDocument();
      expect(screen.getByText(status)).toHaveClass("badge");
      expect(streamLogs).not.toHaveBeenCalled();
    }
  );

  it("renders no idle state when job is running", () => {
    const { container } = render(<JobLiveTab job={{ ...JOB_BASE, status: "running" }} />);
    expect(container.querySelector(".job-live-idle")).not.toBeInTheDocument();
  });

  it("renders the idle badge with plain status for a done job", () => {
    render(<JobLiveTab job={JOB_BASE} />);
    const badge = screen.getByText("done");
    expect(badge.className).toMatch(/^badge /);
    expect(badge.className).not.toMatch(/warn/);
  });

  it("renders the warn-toned unverified badge for a done job with already-satisfied resolution", () => {
    const job = { ...JOB_BASE, status: "done", resolution: "already-satisfied" };
    render(<JobLiveTab job={job} />);
    const badge = screen.getByText("unverified");
    expect(badge.className).toMatch(/badge warn/);
    expect(streamLogs).not.toHaveBeenCalled();
  });
});
