import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { LogsDrawer } from "./LogsDrawer.jsx";
import { getProjectLogs } from "../api.js";

vi.mock("../api.js", () => ({
  getProjectLogs: vi.fn(),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

describe("LogsDrawer", () => {
  it("fetches and renders the log tail for a single-container project on open", async () => {
    getProjectLogs.mockResolvedValue({
      service: "hyqs-solo-app",
      tail: 200,
      lines: ["line one", "line two"],
      truncated: false,
    });

    render(
      <LogsDrawer projectId={7} projectName="solo-app" services={null} onClose={() => {}} />
    );

    expect(await screen.findByText(/line one/)).toBeInTheDocument();
    expect(getProjectLogs).toHaveBeenCalledWith(7, { service: undefined });
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
  });

  it("shows a service selector for compose projects and re-fetches on change", async () => {
    getProjectLogs.mockImplementation((_id, { service }) =>
      Promise.resolve({
        service,
        tail: 200,
        lines: [`${service} log line`],
        truncated: false,
      })
    );

    render(
      <LogsDrawer
        projectId={7}
        projectName="compose-app"
        services={["web", "worker"]}
        onClose={() => {}}
      />
    );

    expect(await screen.findByText("web log line")).toBeInTheDocument();
    expect(getProjectLogs).toHaveBeenCalledWith(7, { service: "web" });

    fireEvent.change(screen.getByRole("combobox"), { target: { value: "worker" } });

    expect(await screen.findByText("worker log line")).toBeInTheDocument();
    expect(getProjectLogs).toHaveBeenCalledWith(7, { service: "worker" });
  });

  it("Refresh re-fetches the current service", async () => {
    getProjectLogs.mockResolvedValue({
      service: "hyqs-solo-app",
      tail: 200,
      lines: ["line one"],
      truncated: false,
    });

    render(
      <LogsDrawer projectId={7} projectName="solo-app" services={null} onClose={() => {}} />
    );

    await screen.findByText("line one");
    expect(getProjectLogs).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByText("Refresh"));

    await waitFor(() => expect(getProjectLogs).toHaveBeenCalledTimes(2));
  });

  it("shows an error state instead of a stuck spinner on fetch failure", async () => {
    getProjectLogs.mockRejectedValue(new Error("boom"));

    render(
      <LogsDrawer projectId={7} projectName="solo-app" services={null} onClose={() => {}} />
    );

    expect(await screen.findByText("Couldn't connect to the server.")).toBeInTheDocument();
  });

  it("shows a truncated hint when the response is truncated", async () => {
    getProjectLogs.mockResolvedValue({
      service: "hyqs-solo-app",
      tail: 5,
      lines: Array.from({ length: 5 }, (_, i) => `line ${i}`),
      truncated: true,
    });

    render(
      <LogsDrawer projectId={7} projectName="solo-app" services={null} onClose={() => {}} />
    );

    expect(await screen.findByText(/most recent lines only/)).toBeInTheDocument();
  });

  it("calls onClose when the close button is clicked", async () => {
    getProjectLogs.mockResolvedValue({
      service: "hyqs-solo-app",
      tail: 200,
      lines: ["line one"],
      truncated: false,
    });
    const onClose = vi.fn();

    render(
      <LogsDrawer projectId={7} projectName="solo-app" services={null} onClose={onClose} />
    );

    await screen.findByText("line one");
    fireEvent.click(screen.getByLabelText("Close"));

    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
