import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, act, fireEvent, waitFor } from "@testing-library/react";
import { ChangelogTab, groupReleasesByDay } from "./ChangelogTab.jsx";

vi.mock("../api.js", () => ({
  getProjectChangelog: vi.fn(() => Promise.resolve({ releases: [] })),
  getDecision: vi.fn(),
}));

import { getProjectChangelog, getDecision } from "../api.js";

const RELEASE_JOB = {
  deployed_commit: "abc123def456",
  previous_commit: "prev789",
  deployed_at: "2026-01-15T10:00:00Z",
  trigger: "pipeline_job",
  verified: true,
  changes: [{ kind: "job", job_id: 42, title: "Add awesome feature", summary: null }],
};

const RELEASE_JOB_WITH_DECISION = {
  ...RELEASE_JOB,
  changes: [
    {
      kind: "job",
      job_id: 43,
      title: "Add another feature",
      summary: null,
      decision_filename: "0043-add-another-feature.md",
    },
  ],
};

const RELEASE_COMMIT = {
  deployed_commit: "def456abc123",
  previous_commit: "prev111",
  deployed_at: "2026-01-14T09:00:00Z",
  trigger: "manual",
  verified: false,
  changes: [{ kind: "commit", sha: "def5678", subject: "Fix typo", author: "Bob" }],
};

describe("ChangelogTab", () => {
  beforeEach(() => {
    vi.mocked(getProjectChangelog).mockResolvedValue({ releases: [] });
    vi.mocked(getDecision).mockReset();
  });

  it("shows 'No releases' when API returns empty list", async () => {
    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.getByText("No releases yet.")).toBeDefined();
  });

  it("shows job title for kind='job' change after expanding release", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    const header = document.querySelector(".changelog-release-header");
    fireEvent.click(header);
    await act(async () => {});

    expect(screen.getByText("Add awesome feature")).toBeDefined();
  });

  it("shows author, subject and sha for kind='commit' change", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_COMMIT],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    const header = document.querySelector(".changelog-release-header");
    fireEvent.click(header);
    await act(async () => {});

    expect(screen.getByText(/Bob/)).toBeDefined();
    expect(screen.getByText(/Fix typo/)).toBeDefined();
    expect(screen.getByText("def5678")).toBeDefined();
  });

  it("shows 'Manual' badge text for trigger='manual' release", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_COMMIT],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.getByText("Manual")).toBeDefined();
  });

  it("hides the trigger badge for the default pipeline_job trigger", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.queryByText("Pipeline")).toBeNull();
  });

  it("shows a human-friendly date in the release header", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.getByText(/15 Jan 2026/)).toBeDefined();
  });

  it("shows the change count in the collapsed release header", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.getByText("1 change")).toBeDefined();
  });

  it("groupReleasesByDay buckets multi-day releases, preserving order", () => {
    const sameDayRelease = { ...RELEASE_JOB, deployed_at: "2026-01-15T18:00:00Z" };
    const groups = groupReleasesByDay([RELEASE_JOB, sameDayRelease, RELEASE_COMMIT]);

    expect(groups).toHaveLength(2);
    expect(groups[0]).toMatchObject({ day: "2026-01-15", releases: [RELEASE_JOB, sameDayRelease] });
    expect(groups[1]).toMatchObject({ day: "2026-01-14", releases: [RELEASE_COMMIT] });
  });

  it("renders one day header per distinct calendar day", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB, RELEASE_COMMIT],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(document.querySelectorAll(".changelog-day-header")).toHaveLength(2);
    expect(screen.getByText(/15 Jan 2026/)).toBeDefined();
    expect(screen.getByText(/14 Jan 2026/)).toBeDefined();
  });

  it("shows the access-denied message when the fetch is forbidden", async () => {
    vi.mocked(getProjectChangelog).mockRejectedValueOnce(new Error("forbidden"));

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(screen.getByText("You don't have access to this data.")).toBeDefined();
  });

  it("shows an error message with Retry, and Retry re-fetches", async () => {
    vi.mocked(getProjectChangelog).mockRejectedValueOnce(new Error("boom"));
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({ releases: [RELEASE_JOB] });

    render(<ChangelogTab projectId={1} />);
    await waitFor(() => expect(screen.getByText("Couldn't connect to the server.")).toBeDefined());

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));

    await waitFor(() => expect(screen.getByText(/15 Jan 2026/)).toBeDefined());
  });

  it("paginates day-groups, revealing more on 'Show earlier'", async () => {
    const releases = [
      RELEASE_JOB, // 2026-01-15
      { ...RELEASE_COMMIT, deployed_at: "2026-01-14T09:00:00Z" }, // day 2
      { ...RELEASE_COMMIT, deployed_at: "2026-01-13T09:00:00Z" }, // day 3
      { ...RELEASE_COMMIT, deployed_at: "2026-01-12T09:00:00Z" }, // day 4
    ];
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({ releases });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    expect(document.querySelectorAll(".changelog-day-header")).toHaveLength(3);
    const showEarlier = screen.getByRole("button", { name: "Show earlier" });

    fireEvent.click(showEarlier);
    await act(async () => {});

    expect(document.querySelectorAll(".changelog-day-header")).toHaveLength(4);
    expect(screen.queryByRole("button", { name: "Show earlier" })).toBeNull();
  });

  it("shows no decision toggle for a job change without decision_filename", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB],
    });

    await act(async () => {
      render(<ChangelogTab projectId={1} />);
    });
    await act(async () => {});

    fireEvent.click(document.querySelector(".changelog-release-header"));
    await act(async () => {});

    expect(document.querySelector(".changelog-decision-toggle")).toBeNull();
  });

  it("shows a decision toggle that fetches and renders the decision on expand", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB_WITH_DECISION],
    });
    vi.mocked(getDecision).mockResolvedValueOnce({ content: "## Why we did this" });

    await act(async () => {
      render(<ChangelogTab projectId={7} />);
    });
    await act(async () => {});

    fireEvent.click(document.querySelector(".changelog-release-header"));
    await act(async () => {});

    const toggle = document.querySelector(".changelog-decision-toggle");
    expect(toggle).not.toBeNull();
    expect(getDecision).not.toHaveBeenCalled();

    fireEvent.click(toggle);
    await act(async () => {});

    expect(getDecision).toHaveBeenCalledTimes(1);
    expect(getDecision).toHaveBeenCalledWith(7, "0043-add-another-feature.md");
    expect(screen.getByText("Why we did this")).toBeDefined();

    fireEvent.click(toggle);
    await act(async () => {});
    fireEvent.click(toggle);
    await act(async () => {});

    expect(getDecision).toHaveBeenCalledTimes(1);
  });

  it("shows a graceful error message when the decision fetch fails", async () => {
    vi.mocked(getProjectChangelog).mockResolvedValueOnce({
      releases: [RELEASE_JOB_WITH_DECISION],
    });
    vi.mocked(getDecision).mockRejectedValueOnce(new Error("boom"));

    await act(async () => {
      render(<ChangelogTab projectId={7} />);
    });
    await act(async () => {});

    fireEvent.click(document.querySelector(".changelog-release-header"));
    await act(async () => {});

    fireEvent.click(document.querySelector(".changelog-decision-toggle"));
    await act(async () => {});

    expect(screen.getByText(/Couldn't load the decision: boom/)).toBeDefined();
  });
});
