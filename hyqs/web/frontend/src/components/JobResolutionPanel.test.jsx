import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { JobResolutionPanel } from "./JobResolutionPanel.jsx";
import { RoleContext } from "../context.js";
import {
  getJobSupervisorEvents,
  getJobDependents,
  requeueJobAtStage,
  fileFixForwardJob,
  patchJobIdea,
  resolveJob,
  retryJob,
  archiveJob,
  createJob,
} from "../api.js";

vi.mock("../api.js", () => ({
  getJobSupervisorEvents: vi.fn(() => Promise.resolve([])),
  getJobDependents: vi.fn(() => Promise.resolve([])),
  requeueJobAtStage: vi.fn(() => Promise.resolve({})),
  fileFixForwardJob: vi.fn(() => Promise.resolve({ job: { id: 900 } })),
  patchJobIdea: vi.fn(() => Promise.resolve({})),
  resolveJob: vi.fn(() => Promise.resolve({})),
  retryJob: vi.fn(() => Promise.resolve({})),
  archiveJob: vi.fn(() => Promise.resolve({})),
  createJob: vi.fn(() => Promise.resolve({ job: { id: 901 } })),
}));

const FAILED_JOB = {
  id: 692,
  idea: "original idea",
  status: "failed",
  failure: "stored XSS in Transaction.currency, rendered via innerHTML in txReadRow",
  error: "security gate rejected",
};

function renderPanel(job = FAILED_JOB, { can = () => true } = {}) {
  return render(
    <RoleContext.Provider
      value={{
        role: "project_admin",
        can,
        authLoading: false,
        authError: false,
        retryAuth: vi.fn(),
      }}
    >
      <JobResolutionPanel job={job} />
    </RoleContext.Provider>
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  getJobSupervisorEvents.mockResolvedValue([]);
  getJobDependents.mockResolvedValue([]);
  window.confirm = vi.fn(() => true);
});

describe("JobResolutionPanel visibility", () => {
  it("renders nothing for a non-failed job", () => {
    const { container } = renderPanel({ ...FAILED_JOB, status: "done" });
    expect(container.firstChild).toBeNull();
  });

  it("renders the full untruncated failure reason behind a title+expander", () => {
    renderPanel();
    const details = document.querySelector(".job-resolution-failure");
    expect(details).toBeTruthy();
    expect(details.querySelector("pre").textContent).toBe(FAILED_JOB.failure);
  });

  it("falls back to job.error when job.failure is absent", () => {
    renderPanel({ ...FAILED_JOB, failure: "" });
    expect(document.querySelector(".job-resolution-failure-full").textContent).toBe(
      FAILED_JOB.error
    );
  });
});

describe("JobResolutionPanel supervisor diagnosis (three-state trio)", () => {
  it("shows a loading state before the fetch resolves", () => {
    getJobSupervisorEvents.mockReturnValue(new Promise(() => {}));
    renderPanel();
    expect(document.querySelector(".spinner")).toBeTruthy();
  });

  it("shows a forbidden message when the fetch is forbidden", async () => {
    getJobSupervisorEvents.mockRejectedValue(new Error("forbidden"));
    renderPanel();
    await waitFor(() => expect(screen.getByText(/don.t have access/i)).toBeInTheDocument());
  });

  it("shows an error + retry button on a generic failure", async () => {
    getJobSupervisorEvents.mockRejectedValue(new Error("network down"));
    renderPanel();
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
  });

  it("renders the latest ai_diagnosed event's diagnosis and confidence", async () => {
    getJobSupervisorEvents.mockResolvedValue([
      {
        action: "ai_diagnosed",
        detail: {
          diagnosis: "Root cause: unsanitized CSV field",
          action: { type: "escalate" },
          confidence: 0.9,
        },
      },
    ]);
    renderPanel();
    await waitFor(() =>
      expect(screen.getByText("Root cause: unsanitized CSV field")).toBeInTheDocument()
    );
    expect(screen.getByText("90% confidence")).toBeInTheDocument();
  });
});

describe("JobResolutionPanel resolution options", () => {
  it("pre-selects the analyst-recommended requeue stage", async () => {
    getJobSupervisorEvents.mockResolvedValue([
      { action: "ai_diagnosed", detail: { action: { type: "requeue_at_stage", stage: "test" } } },
    ]);
    renderPanel();
    const select = await screen.findByDisplayValue(/Test \(recommended\)/);
    expect(select).toBeTruthy();
  });

  it("highlights File-fix-forward and de-emphasizes Requeue when the recommendation is escalate", async () => {
    getJobSupervisorEvents.mockResolvedValue([
      {
        action: "ai_diagnosed",
        detail: { diagnosis: "x", action: { type: "escalate" }, confidence: 0.8 },
      },
    ]);
    renderPanel();
    await waitFor(() => expect(screen.getByText("Recommended")).toBeInTheDocument());
    const requeueBtn = screen.getByText("Requeue");
    expect(requeueBtn.className).toMatch(/btn-secondary/);
  });

  it("calls requeueJobAtStage with the selected stage", async () => {
    renderPanel();
    fireEvent.click(screen.getByText("Requeue"));
    await waitFor(() => expect(requeueJobAtStage).toHaveBeenCalledWith(692, "queued"));
  });

  it("calls fileFixForwardJob with the prefilled idea text and no repoint UI when there are no dependents", async () => {
    renderPanel();
    expect(screen.queryByText(/will be re-pointed/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByText("File a fix-forward job"));
    await waitFor(() => expect(fileFixForwardJob).toHaveBeenCalled());
    expect(fileFixForwardJob.mock.calls[0][1]).toContain(FAILED_JOB.failure);
    expect(fileFixForwardJob.mock.calls[0][3]).toEqual([]);
  });

  it("saving an edited idea patches then retries", async () => {
    renderPanel();
    const textarea = document.querySelector(".job-resolution-idea-edit");
    fireEvent.change(textarea, { target: { value: "a clearer idea" } });
    fireEvent.click(screen.getByText("Save idea & retry"));
    await waitFor(() => expect(patchJobIdea).toHaveBeenCalledWith(692, "a clearer idea"));
    expect(retryJob).toHaveBeenCalledWith(692, { overrideActiveRemediation: false });
  });

  it("mark resolved calls resolveJob", async () => {
    renderPanel();
    fireEvent.click(screen.getByText("Mark resolved"));
    await waitFor(() => expect(resolveJob).toHaveBeenCalledWith(692));
  });

  it("archive requires confirm-click behind a menu before calling archiveJob", async () => {
    renderPanel();
    fireEvent.click(screen.getByText("Archive"));
    const confirmBtn = screen.getByText("Confirm archive");
    fireEvent.click(confirmBtn);
    await waitFor(() => expect(archiveJob).toHaveBeenCalledWith(692));
    expect(window.confirm).toHaveBeenCalled();
  });

  it("hides resolve/fix-forward/requeue options when the caller lacks resolve_job", () => {
    renderPanel(FAILED_JOB, { can: (p) => p === "edit_job_deps" });
    expect(screen.queryByText("Requeue")).not.toBeInTheDocument();
    expect(screen.queryByText("File a fix-forward job")).not.toBeInTheDocument();
    expect(screen.queryByText("Mark resolved")).not.toBeInTheDocument();
    expect(screen.getByText("Save idea & retry")).toBeInTheDocument();
  });

  it("hides archive when the caller lacks archive_job", () => {
    renderPanel(FAILED_JOB, { can: (p) => p === "resolve_job" });
    expect(screen.queryByText("Archive")).not.toBeInTheDocument();
  });
});

describe("JobResolutionPanel needs_split jobs", () => {
  const NEEDS_SPLIT_JOB = { ...FAILED_JOB, needs_split: true, repo_path: "/repo", epic_id: 12 };

  it("renders Refile idea instead of Save idea & retry for a needs_split job", () => {
    renderPanel(NEEDS_SPLIT_JOB);
    expect(screen.getByText("Refile idea")).toBeInTheDocument();
    expect(screen.queryByText("Save idea & retry")).not.toBeInTheDocument();
  });

  it("clicking Refile idea calls createJob with the idea, repo_path, and epic_id, not retryJob/patchJobIdea", async () => {
    renderPanel(NEEDS_SPLIT_JOB);
    fireEvent.click(screen.getByText("Refile idea"));
    await waitFor(() => expect(createJob).toHaveBeenCalledWith(NEEDS_SPLIT_JOB.idea, "/repo", 12));
    expect(retryJob).not.toHaveBeenCalled();
    expect(patchJobIdea).not.toHaveBeenCalled();
  });

  it("clicking Refile idea sends the edited textarea value", async () => {
    renderPanel(NEEDS_SPLIT_JOB);
    const textarea = document.querySelector(".job-resolution-idea-edit");
    fireEvent.change(textarea, { target: { value: "a smaller idea" } });
    fireEvent.click(screen.getByText("Refile idea"));
    await waitFor(() => expect(createJob).toHaveBeenCalledWith("a smaller idea", "/repo", 12));
  });

  it("keeps Save idea & retry for a non-needs_split failed job", () => {
    renderPanel(FAILED_JOB);
    expect(screen.getByText("Save idea & retry")).toBeInTheDocument();
    expect(screen.queryByText("Refile idea")).not.toBeInTheDocument();
  });
});

describe("JobResolutionPanel fix-forward dependent re-pointing", () => {
  const DEPENDENTS = [
    { id: 693, title: "downstream a", status: "pending" },
    { id: 694, title: "downstream b", status: "pending" },
  ];

  it("renders a checked-by-default list of dependents that will be re-pointed", async () => {
    getJobDependents.mockResolvedValue(DEPENDENTS);
    renderPanel();
    await waitFor(() =>
      expect(
        screen.getByText("2 dependent job(s) will be re-pointed to the new job")
      ).toBeInTheDocument()
    );
    expect(screen.getByText(/downstream a/)).toBeInTheDocument();
    expect(screen.getByText(/downstream b/)).toBeInTheDocument();
    const checkboxes = document.querySelectorAll(
      ".job-resolution-repoint-item input[type=checkbox]"
    );
    expect(checkboxes.length).toBe(2);
    checkboxes.forEach((cb) => expect(cb.checked).toBe(true));
  });

  it("submits only the checked dependent ids after unchecking one", async () => {
    getJobDependents.mockResolvedValue(DEPENDENTS);
    renderPanel();
    await waitFor(() => expect(screen.getByText(/downstream a/)).toBeInTheDocument());
    const checkboxes = document.querySelectorAll(
      ".job-resolution-repoint-item input[type=checkbox]"
    );
    fireEvent.click(checkboxes[0]);

    fireEvent.click(screen.getByText("File a fix-forward job"));

    await waitFor(() => expect(fileFixForwardJob).toHaveBeenCalled());
    expect(fileFixForwardJob.mock.calls[0][3]).toEqual([694]);
  });

  it("surfaces an error + retry instead of silently treating a failed dependents fetch as 'no dependents'", async () => {
    getJobDependents.mockRejectedValue(new Error("network down"));
    renderPanel();
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
    expect(screen.queryByText(/will be re-pointed/)).not.toBeInTheDocument();
    expect(screen.getByText("File a fix-forward job")).toBeDisabled();
  });

  it("re-fetches dependents when Retry is clicked after a failed fetch", async () => {
    getJobDependents.mockRejectedValueOnce(new Error("network down"));
    getJobDependents.mockResolvedValueOnce(DEPENDENTS);
    renderPanel();
    await waitFor(() => expect(screen.getByText("Retry")).toBeInTheDocument());
    fireEvent.click(screen.getByText("Retry"));
    await waitFor(() => expect(screen.getByText(/downstream a/)).toBeInTheDocument());
    expect(screen.getByText("File a fix-forward job")).not.toBeDisabled();
  });

  it("disables filing a fix-forward job while the dependents fetch is forbidden", async () => {
    getJobDependents.mockRejectedValue(new Error("forbidden"));
    renderPanel();
    await waitFor(() => expect(screen.getByText(/don.t have access/i)).toBeInTheDocument());
    expect(screen.getByText("File a fix-forward job")).toBeDisabled();
  });
});

describe("JobResolutionPanel active-remediation conflict override", () => {
  function conflictError() {
    return Object.assign(new Error("conflict message"), {
      status: 409,
      body: { active_remediation_job_id: 900 },
    });
  }

  it("shows an override-confirm block instead of a toast when fileFixForwardJob hits a 409 conflict", async () => {
    fileFixForwardJob.mockRejectedValueOnce(conflictError());
    fileFixForwardJob.mockResolvedValueOnce({ job: { id: 800 } });
    renderPanel();

    fireEvent.click(screen.getByText("File a fix-forward job"));
    await waitFor(() => expect(screen.getByText("conflict message")).toBeInTheDocument());
    expect(screen.getByText("Override and proceed")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Override and proceed"));
    await waitFor(() => expect(fileFixForwardJob).toHaveBeenCalledTimes(2));
    expect(fileFixForwardJob.mock.calls[1][4]).toBe(true);
    await waitFor(() => expect(screen.queryByText("conflict message")).not.toBeInTheDocument());
  });

  it("dismisses the fix-forward conflict block on Cancel without calling the API again", async () => {
    fileFixForwardJob.mockRejectedValueOnce(conflictError());
    renderPanel();

    fireEvent.click(screen.getByText("File a fix-forward job"));
    await waitFor(() => expect(screen.getByText("conflict message")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByText("conflict message")).not.toBeInTheDocument();
    expect(fileFixForwardJob).toHaveBeenCalledTimes(1);
  });

  it("shows an override-confirm block instead of a toast when retryJob hits a 409 conflict", async () => {
    retryJob.mockRejectedValueOnce(conflictError());
    retryJob.mockResolvedValueOnce({});
    renderPanel();

    fireEvent.click(screen.getByText("Save idea & retry"));
    await waitFor(() => expect(screen.getByText("conflict message")).toBeInTheDocument());
    expect(screen.getByText("Override and proceed")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Override and proceed"));
    await waitFor(() => expect(retryJob).toHaveBeenCalledTimes(2));
    expect(retryJob.mock.calls[1][1]).toEqual({ overrideActiveRemediation: true });
    await waitFor(() => expect(screen.queryByText("conflict message")).not.toBeInTheDocument());
  });

  it("dismisses the retry conflict block on Cancel without calling the API again", async () => {
    retryJob.mockRejectedValueOnce(conflictError());
    renderPanel();

    fireEvent.click(screen.getByText("Save idea & retry"));
    await waitFor(() => expect(screen.getByText("conflict message")).toBeInTheDocument());

    fireEvent.click(screen.getByText("Cancel"));
    expect(screen.queryByText("conflict message")).not.toBeInTheDocument();
    expect(retryJob).toHaveBeenCalledTimes(1);
  });
});
