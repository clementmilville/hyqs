import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { JobChatPanel } from "./JobChatPanel.jsx";
import { ToastProvider } from "./Toast.jsx";
import { RoleContext } from "../context.js";

// streamJobChat immediately calls onToolUse then onResult with 2 proposed jobs when send fires
vi.mock("../api.js", () => ({
  streamJobChat: vi.fn((projectId, messages, epicId, cbs) => {
    Promise.resolve().then(() => {
      cbs.onToolUse?.({ tool: "Grep", input_summary: "Grep foo" });
      cbs.onResult({
        jobs: [
          { title: "Job A", description: "desc A" },
          { title: "Job B", description: "desc B" },
        ],
      });
    });
    return { cancel: vi.fn() };
  }),
  createJob: vi.fn(),
  createBatchJobs: vi.fn().mockResolvedValue({}),
  suggestEpicForJob: vi.fn().mockResolvedValue({}),
}));

import { createJob, createBatchJobs, streamJobChat } from "../api.js";

const roleCtx = {
  role: "admin",
  can: () => true,
  setPermissions: () => {},
  authLoading: false,
  authError: false,
  retryAuth: () => {},
};

function renderPanel(props = {}) {
  return render(
    <RoleContext.Provider value={roleCtx}>
      <ToastProvider>
        <JobChatPanel
          projectId={1}
          projectRepo="/repo"
          epicId={null}
          epics={[]}
          onJobCreated={vi.fn()}
          {...props}
        />
      </ToastProvider>
    </RoleContext.Provider>
  );
}

describe("JobChatPanel sessionStorage persistence", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createJob.mockResolvedValue({ job: { id: 1 }, auto_epic: { name: "General" } });
    createBatchJobs.mockResolvedValue({});
    sessionStorage.clear();
  });

  it("persists messages to sessionStorage after a send completes", async () => {
    renderPanel({ projectId: 1, epicId: null });

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "Add feature" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await screen.findByRole("button", { name: /create 2 jobs/i });

    await waitFor(() => {
      const raw = sessionStorage.getItem("jobchat:1:none");
      expect(raw).not.toBeNull();
      const stored = JSON.parse(raw);
      expect(stored.messages).toContainEqual(
        expect.objectContaining({ role: "user", content: "Add feature" })
      );
    });
  });

  it("restores messages from sessionStorage on mount", () => {
    sessionStorage.setItem(
      "jobchat:1:none",
      JSON.stringify({
        messages: [
          { role: "user", content: "Hello from storage" },
          { role: "assistant", content: "Hi there" },
        ],
        proposedJobs: [],
        draft: "",
      })
    );

    renderPanel({ projectId: 1, epicId: null });

    expect(screen.getByText("Hello from storage")).toBeInTheDocument();
    expect(screen.getByText(/Hi there/)).toBeInTheDocument();
  });

  it("drops pending bubbles when restoring from sessionStorage", () => {
    sessionStorage.setItem(
      "jobchat:1:none",
      JSON.stringify({
        messages: [
          { role: "user", content: "Hello" },
          { role: "assistant", content: "half-streamed", pending: true },
        ],
        proposedJobs: [],
        draft: "",
      })
    );

    renderPanel({ projectId: 1, epicId: null });

    expect(screen.getByText("Hello")).toBeInTheDocument();
    expect(screen.queryByText("half-streamed")).not.toBeInTheDocument();
  });

  it("clears sessionStorage after confirmAll succeeds", async () => {
    renderPanel({ projectId: 1, epicId: null });

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "Add feature" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    const confirmBtn = await screen.findByRole("button", { name: /create 2 jobs/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(sessionStorage.getItem("jobchat:1:none")).toBeNull();
    });
  });
});

describe("JobChatPanel image attach", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  function makeImageFile(type = "image/png", size = 100) {
    const blob = new Blob([new Uint8Array(size)], { type });
    return new File([blob], "test.png", { type });
  }

  it("shows thumbnail preview after selecting a valid image", async () => {
    renderPanel();

    const input = document.querySelector("input[type=file]");
    const file = makeImageFile("image/png", 100);

    // FileReader.readAsDataURL is synchronous in jsdom via mocking
    const originalFR = global.FileReader;
    class MockFileReader {
      readAsDataURL() {
        this.onload({ target: { result: "data:image/png;base64,abc123" } });
      }
    }
    global.FileReader = MockFileReader;

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByRole("img", { name: "staged" })).toBeInTheDocument();
    });

    global.FileReader = originalFR;
  });

  it("shows error toast and stages nothing for disallowed MIME type", async () => {
    renderPanel();

    const input = document.querySelector("input[type=file]");
    const file = new File([new Uint8Array(10)], "test.pdf", { type: "application/pdf" });

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByText(/Only JPEG/i)).toBeInTheDocument();
    });
    expect(document.querySelector(".job-chat-preview-thumb")).toBeNull();
  });

  it("shows error toast and stages nothing for oversized file", async () => {
    renderPanel();

    const input = document.querySelector("input[type=file]");
    const bigFile = makeImageFile("image/jpeg", 6 * 1024 * 1024);

    fireEvent.change(input, { target: { files: [bigFile] } });

    await waitFor(() => {
      expect(screen.getByText(/5 MB/i)).toBeInTheDocument();
    });
    expect(document.querySelector(".job-chat-preview-thumb")).toBeNull();
  });

  it("removes thumbnail when ✕ button is clicked", async () => {
    renderPanel();

    const input = document.querySelector("input[type=file]");
    const file = makeImageFile("image/png", 100);

    const originalFR = global.FileReader;
    class MockFileReader {
      readAsDataURL() {
        this.onload({ target: { result: "data:image/png;base64,abc123" } });
      }
    }
    global.FileReader = MockFileReader;

    fireEvent.change(input, { target: { files: [file] } });

    const removeBtn = await screen.findByRole("button", { name: /remove image/i });
    fireEvent.click(removeBtn);

    expect(document.querySelector(".job-chat-preview-thumb")).toBeNull();
    global.FileReader = originalFR;
  });

  it("sends content as array with image block and clears staged image after send", async () => {
    const { streamJobChat } = await import("../api.js");

    renderPanel();

    const input = document.querySelector("input[type=file]");
    const file = makeImageFile("image/png", 100);

    const originalFR = global.FileReader;
    class MockFileReader {
      readAsDataURL() {
        this.onload({ target: { result: "data:image/png;base64,abc123" } });
      }
    }
    global.FileReader = MockFileReader;

    fireEvent.change(input, { target: { files: [file] } });
    await screen.findByRole("img", { name: "staged" });

    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "Here is my image" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    await waitFor(() => {
      const call = streamJobChat.mock.calls[0];
      const msgs = call[1];
      const last = msgs[msgs.length - 1];
      expect(Array.isArray(last.content)).toBe(true);
      expect(last.content[0].type).toBe("image");
      expect(last.content[0].source.data).toBe("abc123");
      expect(last.content[1].type).toBe("text");
      expect(last.content[1].text).toBe("Here is my image");
    });

    // thumbnail cleared after send
    expect(document.querySelector(".job-chat-preview-thumb")).toBeNull();
    global.FileReader = originalFR;
  });

  it("renders inline image in chat thread for array-content user messages", () => {
    sessionStorage.setItem(
      "jobchat:1:none",
      JSON.stringify({
        messages: [
          {
            role: "user",
            content: [
              {
                type: "image",
                source: { type: "base64", media_type: "image/png", data: "abc123" },
              },
              { type: "text", text: "Look at this" },
            ],
          },
        ],
        proposedJobs: [],
        draft: "",
      })
    );

    renderPanel({ projectId: 1, epicId: null });

    const img = document.querySelector(".job-chat-inline-img");
    expect(img).not.toBeNull();
    expect(img.src).toContain("data:image/png;base64,abc123");
    expect(screen.getByText("Look at this")).toBeInTheDocument();
  });
});

describe("JobChatPanel double-submit guard", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    createBatchJobs.mockResolvedValue({});
  });

  it("rapid double-click on confirm button calls createBatchJobs once for the whole batch", async () => {
    renderPanel();

    // Trigger send to seed proposed jobs via the mocked onResult
    const textarea = screen.getByRole("textbox");
    fireEvent.change(textarea, { target: { value: "Add feature" } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });

    // Wait for the 2 proposed jobs to appear
    const confirmBtn = await screen.findByRole("button", { name: /create 2 jobs/i });

    // Simulate rapid double-click before re-render (both in the same synchronous tick)
    fireEvent.click(confirmBtn);
    fireEvent.click(confirmBtn);

    // createBatchJobs should be called exactly once (the whole batch in one call)
    await waitFor(() => {
      expect(createBatchJobs).toHaveBeenCalledTimes(1);
    });
  });
});

describe("JobChatPanel project isolation", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
  });

  it("does not show project A messages when switched to project B and does not cross-write B's key", async () => {
    sessionStorage.setItem(
      "jobchat:1:none",
      JSON.stringify({
        messages: [{ role: "user", content: "Project A message" }],
        proposedJobs: [],
        draft: "",
      })
    );

    renderPanel({ projectId: 1, epicId: null });
    expect(screen.getByText("Project A message")).toBeInTheDocument();

    cleanup();

    renderPanel({ projectId: 2, epicId: null });
    expect(screen.queryByText("Project A message")).not.toBeInTheDocument();
    expect(sessionStorage.getItem("jobchat:2:none")).toBeNull();
  });
});

describe("JobChatPanel new-job intake paths", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    sessionStorage.clear();
    createBatchJobs.mockResolvedValue({});
    streamJobChat.mockImplementation((projectId, messages, epicId, cbs) => {
      Promise.resolve().then(() => {
        cbs.onResult({
          jobs: [
            {
              title: "New feature",
              description: "Build the thing",
              acceptance_criteria: "It works",
              alternatives: [
                {
                  title: "Opt A",
                  description: "Simple approach",
                  acceptance_criteria: "Works simply",
                },
                {
                  title: "Opt B",
                  description: "Complex approach",
                  acceptance_criteria: "Works fully",
                },
              ],
            },
          ],
        });
      });
      return { cancel: vi.fn() };
    });
  });

  function sendMessage(text = "Add feature") {
    const textarea = screen.getByPlaceholderText(
      "Describe what to build… (Enter to send, Shift+Enter for newline)"
    );
    fireEvent.change(textarea, { target: { value: text } });
    fireEvent.keyDown(textarea, { key: "Enter", code: "Enter" });
  }

  it("renders IdeationPanel option cards when chat response includes alternatives", async () => {
    renderPanel();
    sendMessage();

    const radios = await screen.findAllByRole("radio");
    expect(radios).toHaveLength(2);
    expect(screen.getByText("Opt A")).toBeInTheDocument();
    expect(screen.getByText("Opt B")).toBeInTheDocument();
  });

  it("selecting an alternative option updates the panel edit fields", async () => {
    renderPanel();
    sendMessage();

    const radios = await screen.findAllByRole("radio");
    fireEvent.click(radios[0]);

    await waitFor(() => {
      const titleInput = document.querySelector(".ideation-title");
      expect(titleInput.value).toBe("Opt A");
    });
  });

  it("confirm step passes the selected epic_id to createBatchJobs", async () => {
    const epics = [{ id: 5, name: "Sprint 1", archived: false }];
    renderPanel({ epics, epicId: 5 });
    sendMessage();

    await screen.findByRole("button", { name: /create 1 job/i });

    fireEvent.click(screen.getByRole("button", { name: /create 1 job/i }));

    await waitFor(() => {
      expect(createBatchJobs).toHaveBeenCalledWith(
        "/repo",
        expect.arrayContaining([expect.objectContaining({ epic_id: 5 })]),
        null
      );
    });
  });

  it("successful job creation calls onJobCreated after createBatchJobs resolves", async () => {
    const onJobCreated = vi.fn();
    renderPanel({ onJobCreated });
    sendMessage();

    const confirmBtn = await screen.findByRole("button", { name: /create 1 job/i });
    fireEvent.click(confirmBtn);

    await waitFor(() => {
      expect(onJobCreated).toHaveBeenCalledTimes(1);
    });
  });
});
