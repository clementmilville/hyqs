import { describe, it, expect, vi, beforeAll, beforeEach, afterEach } from "vitest";
import { render, act, fireEvent, screen } from "@testing-library/react";
import { IntakeView } from "./IntakeView.jsx";
import {
  setDraftSpec as mockSetDraftSpec,
  listIntakeSessions,
  getIntakeSession,
  abandonIntake,
  createIntakeSession,
  confirmIntake,
} from "../api.js";

// jsdom doesn't implement scrollIntoView
beforeAll(() => {
  window.HTMLElement.prototype.scrollIntoView = vi.fn();
});

vi.mock("../api.js", () => ({
  createIntakeSession: vi.fn(() =>
    Promise.resolve({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    })
  ),
  listIntakeSessions: vi.fn(() => Promise.resolve({ sessions: [] })),
  getIntakeSession: vi.fn(),
  confirmIntake: vi.fn(),
  streamIntakeMessage: vi.fn(),
  abandonIntake: vi.fn(() => Promise.resolve({})),
  setDraftSpec: vi.fn(() =>
    Promise.resolve({
      session: {
        session_id: "test-session-1",
        draft_spec: { name: "New Name" },
        updated_at: "2024-01-01T00:01:00+00:00",
      },
      missing: ["slug", "one_liner", "problem", "users", "auth", "features", "data_model", "stack"],
    })
  ),
}));

// Controlled mock: call mockStream(state) to drive what useAIStream returns.
let currentStreamState = {
  streaming: false,
  reconnecting: false,
  text: "",
  toolEvents: [],
  error: null,
  result: null,
  usage: null,
  start: vi.fn(),
  stop: vi.fn(),
};

vi.mock("../hooks/useAIStream.js", () => ({
  useAIStream: () => currentStreamState,
}));

describe("IntakeView cancel behavior", () => {
  beforeEach(() => {
    vi.mocked(listIntakeSessions).mockResolvedValue({ sessions: [] });
    vi.mocked(createIntakeSession).mockResolvedValue({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    });
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  it("calls onCancel when the back button is clicked and onCancel is provided", async () => {
    const onCancelSpy = vi.fn();
    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={onCancelSpy} />);
    });
    await act(async () => {});

    const backBtn = document.querySelector(".intake-back-btn");
    expect(backBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(backBtn);
    });

    expect(onCancelSpy).toHaveBeenCalledOnce();
  });

  it("calls onNavWorkspace(null, 'dashboard') when onCancel is omitted", async () => {
    const onNavWorkspaceSpy = vi.fn();
    await act(async () => {
      render(<IntakeView onNavWorkspace={onNavWorkspaceSpy} />);
    });
    await act(async () => {});

    const backBtn = document.querySelector(".intake-back-btn");
    expect(backBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(backBtn);
    });

    expect(onNavWorkspaceSpy).toHaveBeenCalledWith(null, "dashboard");
  });
});

describe("IntakeView accessible labels", () => {
  beforeEach(() => {
    vi.mocked(listIntakeSessions).mockResolvedValue({ sessions: [] });
    vi.mocked(createIntakeSession).mockResolvedValue({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    });
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  it("chat message input is reachable via its accessible label", async () => {
    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    expect(screen.getByLabelText("Message")).toBe(document.querySelector(".intake-form input"));
  });

  it("skip-form's project-name input is reachable via its accessible label", async () => {
    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    await act(async () => {
      fireEvent.click(document.querySelector(".skip-interview-toggle"));
    });

    expect(screen.getByLabelText("Project name")).toBe(document.getElementById("skip-name-main"));
  });
});

describe("IntakeView stepper", () => {
  it("renders 6 phase steps all marked step-remaining when no phase received", async () => {
    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    const steps = document.querySelectorAll(".intake-step");
    expect(steps).toHaveLength(6);
    steps.forEach((s) => {
      expect(s.classList.contains("step-remaining")).toBe(true);
    });
  });

  it("marks Vision and Auth & Access done, Features active, rest remaining", async () => {
    // Simulate: streaming was true, now false with a result carrying phase='Features'
    const resultPayload = {
      text: "Tell me about your features.",
      draft_spec: {},
      missing: ["features", "data_model", "stack"],
      phase: "Features",
    };

    // Step 1: streaming=true (no result yet)
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    let { rerender } = render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    // Wait for listIntakeSessions + createIntakeSession promises
    await act(async () => {});

    // Step 2: streaming=false with result — triggers the effect
    currentStreamState = { ...currentStreamState, streaming: false, result: resultPayload };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    const steps = document.querySelectorAll(".intake-step");
    expect(steps).toHaveLength(6);

    // Vision (index 0) → done
    expect(steps[0].classList.contains("step-done")).toBe(true);
    expect(steps[0].textContent).toContain("Vision");

    // Auth & Access (index 1) → done
    expect(steps[1].classList.contains("step-done")).toBe(true);
    expect(steps[1].textContent).toContain("Auth & Access");

    // Features (index 2) → active
    expect(steps[2].classList.contains("step-active")).toBe(true);
    expect(steps[2].textContent).toContain("Features");

    // Data & Integrations (index 3) → remaining
    expect(steps[3].classList.contains("step-remaining")).toBe(true);

    // Constraints (index 4) → remaining
    expect(steps[4].classList.contains("step-remaining")).toBe(true);

    // Stack & Review (index 5) → remaining
    expect(steps[5].classList.contains("step-remaining")).toBe(true);
  });
});

describe("IntakeView editable spec", () => {
  beforeEach(() => {
    vi.mocked(mockSetDraftSpec).mockClear();
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  it("calls setDraftSpec with correct args when a scalar field is edited and blurred", async () => {
    // Render with streaming=true then flip to streaming=false with a draft_spec result
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {});

    // Flip streaming off with a populated draft_spec
    currentStreamState = {
      ...currentStreamState,
      streaming: false,
      result: {
        text: "Tell me about your project.",
        draft_spec: { name: "My App" },
        missing: [
          "slug",
          "one_liner",
          "problem",
          "users",
          "auth",
          "features",
          "data_model",
          "stack",
        ],
        phase: "Vision",
        session_updated_at: "2024-01-01T00:00:00+00:00",
      },
    };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    // Open the spec panel
    const specHead = document.querySelector(".intake-spec-head");
    await act(async () => {
      fireEvent.click(specHead);
    });

    // Click the first editable field value (name field)
    const editableVals = document.querySelectorAll(".spec-field-val.editable");
    expect(editableVals.length).toBeGreaterThan(0);
    await act(async () => {
      fireEvent.click(editableVals[0]);
    });

    // An input should now be visible
    const input = document.querySelector(".spec-field-input");
    expect(input).not.toBeNull();

    // Type a new value
    await act(async () => {
      fireEvent.change(input, { target: { value: "New Name" } });
    });

    // Blur to commit
    await act(async () => {
      fireEvent.blur(input);
    });
    // Let async commitField resolve
    await act(async () => {});

    expect(mockSetDraftSpec).toHaveBeenCalledWith(
      "test-session-1",
      { name: "New Name" },
      "2024-01-01T00:00:00+00:00"
    );
  });

  it("calls setDraftSpec with features array when a feature is added", async () => {
    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    // Open the spec panel
    const specHead = document.querySelector(".intake-spec-head");
    await act(async () => {
      fireEvent.click(specHead);
    });

    // Click the Add feature button
    const addBtn = document.querySelector(".add-feature-btn");
    expect(addBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(addBtn);
    });

    // Fill in the title field (first input in the feature form)
    const featureInputs = document.querySelectorAll(".feature-form input");
    expect(featureInputs.length).toBeGreaterThan(0);
    await act(async () => {
      fireEvent.change(featureInputs[0], { target: { value: "Login" } });
    });

    // Click Save
    const saveBtn = document.querySelector(".feature-form button");
    await act(async () => {
      fireEvent.click(saveBtn);
    });
    await act(async () => {});

    expect(mockSetDraftSpec).toHaveBeenCalledWith(
      "test-session-1",
      expect.objectContaining({
        features: expect.arrayContaining([expect.objectContaining({ title: "Login" })]),
      }),
      "2024-01-01T00:00:00+00:00"
    );
  });

  it("calls setDraftSpec with empty features when a feature is removed", async () => {
    // Start with a populated draft_spec that has features
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {});

    currentStreamState = {
      ...currentStreamState,
      streaming: false,
      result: {
        text: "Great.",
        draft_spec: {
          features: [
            {
              title: "Login",
              description: "D",
              acceptance_criteria: "A",
              theme: "core",
              source: "user",
            },
          ],
        },
        missing: ["name", "slug", "one_liner", "problem", "users", "auth", "data_model", "stack"],
        phase: null,
        session_updated_at: "2024-01-01T00:00:00+00:00",
      },
    };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    // Open spec panel
    const specHead = document.querySelector(".intake-spec-head");
    await act(async () => {
      fireEvent.click(specHead);
    });

    // Click Remove on the first feature
    const removeBtn = document.querySelector(".feat-remove-btn");
    expect(removeBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(removeBtn);
    });
    await act(async () => {});

    expect(mockSetDraftSpec).toHaveBeenCalledWith(
      "test-session-1",
      { features: [] },
      "2024-01-01T00:00:00+00:00"
    );
  });
});

const SUGGESTED_FEATURE = {
  title: "AI assistant",
  description: "D",
  acceptance_criteria: "A",
  theme: "core",
  source: "suggested",
};

function seedSuggestedFeature(rerender) {
  currentStreamState = {
    ...currentStreamState,
    streaming: false,
    result: {
      text: "Here are some suggestions.",
      draft_spec: { features: [SUGGESTED_FEATURE] },
      missing: ["name", "slug", "one_liner", "problem", "users", "auth", "data_model", "stack"],
      phase: null,
      session_updated_at: "2024-01-01T00:00:00+00:00",
    },
  };
  return act(async () => {
    rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
  });
}

describe("IntakeView suggested features", () => {
  beforeEach(() => {
    vi.mocked(mockSetDraftSpec).mockClear();
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  it("renders a suggested feature with feat-suggested class and Suggested badge", async () => {
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {});
    await seedSuggestedFeature(rerender);

    await act(async () => {
      fireEvent.click(document.querySelector(".intake-spec-head"));
    });

    const row = document.querySelector(".spec-feature-row");
    expect(row.classList.contains("feat-suggested")).toBe(true);
    expect(row.textContent).toContain("Suggested");
  });

  it("clicking Accept promotes source to user via setDraftSpec", async () => {
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {});
    await seedSuggestedFeature(rerender);

    await act(async () => {
      fireEvent.click(document.querySelector(".intake-spec-head"));
    });

    const acceptBtn = document.querySelector(".feat-accept-btn");
    expect(acceptBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(acceptBtn);
    });
    await act(async () => {});

    expect(mockSetDraftSpec).toHaveBeenCalledWith(
      "test-session-1",
      { features: [{ ...SUGGESTED_FEATURE, source: "user" }] },
      "2024-01-01T00:00:00+00:00"
    );
  });

  it("clicking Reject removes the suggested feature via setDraftSpec", async () => {
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {});
    await seedSuggestedFeature(rerender);

    await act(async () => {
      fireEvent.click(document.querySelector(".intake-spec-head"));
    });

    const rejectBtn = document.querySelector(".feat-reject-btn");
    expect(rejectBtn).not.toBeNull();
    await act(async () => {
      fireEvent.click(rejectBtn);
    });
    await act(async () => {});

    expect(mockSetDraftSpec).toHaveBeenCalledWith(
      "test-session-1",
      { features: [] },
      "2024-01-01T00:00:00+00:00"
    );
  });
});

describe("IntakeView resume/abandon", () => {
  beforeEach(() => {
    vi.mocked(listIntakeSessions).mockReset();
    vi.mocked(getIntakeSession).mockReset();
    vi.mocked(abandonIntake).mockReset();
    vi.mocked(createIntakeSession).mockReset();
    vi.mocked(createIntakeSession).mockResolvedValue({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    });
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  it("renders picker row with draft name and progress badge when listIntakeSessions returns an in_progress session", async () => {
    vi.mocked(listIntakeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "s1",
          status: "in_progress",
          draft_spec: { name: "My App", slug: "my-app" },
          updated_at: "2024-01-01T00:00:00+00:00",
          messages: [],
        },
      ],
    });

    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    const row = document.querySelector(".picker-row");
    expect(row).not.toBeNull();
    expect(document.querySelector(".picker-name").textContent).toBe("My App");
    // name + slug = 2 filled out of 9
    expect(document.querySelector(".picker-progress").textContent).toContain("2/9");
  });

  it("clicking Resume calls getIntakeSession and transitions to chat with hydrated messages", async () => {
    vi.mocked(listIntakeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "s1",
          status: "in_progress",
          draft_spec: { name: "My App" },
          updated_at: "2024-01-01T00:00:00+00:00",
          messages: [],
        },
      ],
    });
    vi.mocked(getIntakeSession).mockResolvedValue({
      session: {
        session_id: "s1",
        draft_spec: { name: "My App" },
        updated_at: "2024-01-01T00:00:00+00:00",
        messages: [
          { role: "user", content: "Hello" },
          { role: "assistant", content: "Hi there" },
        ],
      },
    });

    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    const resumeBtn = document.querySelector(".picker-resume-btn");
    expect(resumeBtn).not.toBeNull();

    await act(async () => {
      fireEvent.click(resumeBtn);
    });
    await act(async () => {});

    expect(getIntakeSession).toHaveBeenCalledWith("s1");
    // Picker should be gone, chat body visible
    expect(document.querySelector(".picker-row")).toBeNull();
    expect(document.querySelector(".messages")).not.toBeNull();
    // Hydrated messages should be rendered
    const msgs = document.querySelectorAll(".msg");
    expect(msgs.length).toBe(2);
  });

  it("resumeSession uses the session's authoritative missing_required rather than recomputing client-side", async () => {
    vi.mocked(listIntakeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "s1",
          status: "in_progress",
          draft_spec: { name: "My App", data_model: {} },
          updated_at: "2024-01-01T00:00:00+00:00",
          messages: [],
        },
      ],
    });
    // data_model: {} is truthy, so a naive client-side truthiness check would
    // not flag it — only the server's check_completeness knows it's empty.
    vi.mocked(getIntakeSession).mockResolvedValue({
      session: {
        session_id: "s1",
        draft_spec: { name: "My App", data_model: {} },
        updated_at: "2024-01-01T00:00:00+00:00",
        messages: [],
        missing_required: ["data_model", "slug"],
      },
    });

    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    await act(async () => {
      fireEvent.click(document.querySelector(".picker-resume-btn"));
    });
    await act(async () => {});

    const req = document.querySelector(".intake-requirements");
    expect(req.textContent).toContain("data_model");
    expect(req.textContent).toContain("slug");
    expect(document.querySelector(".create-project-btn").disabled).toBe(true);
  });

  it("clicking Abandon calls abandonIntake, removes the row, and calls createIntakeSession when it was the last", async () => {
    vi.mocked(listIntakeSessions).mockResolvedValue({
      sessions: [
        {
          session_id: "s1",
          status: "in_progress",
          draft_spec: {},
          updated_at: "2024-01-01T00:00:00+00:00",
          messages: [],
        },
      ],
    });
    vi.mocked(abandonIntake).mockResolvedValue({});

    await act(async () => {
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    await act(async () => {});

    expect(document.querySelector(".picker-row")).not.toBeNull();

    const abandonBtn = document.querySelector(".picker-abandon-btn");
    await act(async () => {
      fireEvent.click(abandonBtn);
    });
    await act(async () => {});

    expect(abandonIntake).toHaveBeenCalledWith("s1");
    // Row is gone
    expect(document.querySelector(".picker-row")).toBeNull();
    // Last row → createIntakeSession called to start a fresh session
    expect(createIntakeSession).toHaveBeenCalled();
    // Chat body rendered after transition
    expect(document.querySelector(".messages")).not.toBeNull();
  });

  it("shows Retry button when aiError is set and clicking it re-triggers start with the last message", async () => {
    vi.mocked(listIntakeSessions).mockResolvedValue({ sessions: [] });

    const startMock = vi.fn();
    currentStreamState = { ...currentStreamState, start: startMock };

    const { rerender } = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {}); // createIntakeSession resolves

    // Send a message to populate lastMsgRef
    const input = document.querySelector(".intake-form input");
    await act(async () => {
      fireEvent.change(input, { target: { value: "Hello" } });
    });
    await act(async () => {
      fireEvent.submit(document.querySelector(".intake-form"));
    });

    expect(startMock).toHaveBeenCalledWith(expect.any(Function), "test-session-1", "Hello");
    startMock.mockClear();

    // Simulate stream ending with an error
    currentStreamState = {
      ...currentStreamState,
      streaming: false,
      error: new Error("connection lost"),
      result: null,
    };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });

    const retryBtn = document.querySelector(".retry-btn");
    expect(retryBtn).not.toBeNull();
    expect(retryBtn.textContent).toBe("Retry");

    await act(async () => {
      fireEvent.click(retryBtn);
    });

    expect(startMock).toHaveBeenCalledWith(expect.any(Function), "test-session-1", "Hello");
  });
});

const CONFIRM_RESPONSE = {
  project: { id: 42, name: "My App" },
  board_url: "/projects/42",
  plan: {
    epics: [{ name: "Core" }],
    jobs: [{ title: "Build login", epic: "Core", is_foundation: false, priority: 10 }],
  },
};

describe("IntakeView auto-handoff", () => {
  const onNavMock = vi.fn();

  beforeEach(() => {
    vi.useFakeTimers();
    vi.mocked(confirmIntake).mockResolvedValue(CONFIRM_RESPONSE);
    vi.mocked(listIntakeSessions).mockResolvedValue({ sessions: [] });
    vi.mocked(createIntakeSession).mockResolvedValue({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    });
    onNavMock.mockClear();
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.mocked(confirmIntake).mockReset();
  });

  async function renderAndInit() {
    const utils = await act(async () =>
      render(<IntakeView onNavWorkspace={onNavMock} onCancel={vi.fn()} />)
    );
    await act(async () => {}); // let createIntakeSession resolve
    return utils;
  }

  async function triggerComplete(rerender) {
    currentStreamState = {
      ...currentStreamState,
      result: {
        text: "Spec is complete!",
        draft_spec: { name: "My App" },
        missing: [],
        phase: "Stack & Review",
        complete: true,
      },
    };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={onNavMock} onCancel={vi.fn()} />);
    });
  }

  it("shows countdown banner before 5s and does not call confirmIntake", async () => {
    const { rerender } = await renderAndInit();

    await triggerComplete(rerender);

    await act(async () => {
      vi.advanceTimersByTime(4000);
    });

    expect(document.querySelector(".intake-countdown-banner")).not.toBeNull();
    expect(confirmIntake).not.toHaveBeenCalled();
  });

  it("calls confirmIntake exactly once after 5s and renders receipt", async () => {
    const { rerender } = await renderAndInit();

    await triggerComplete(rerender);

    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    // Let confirmIntake promise resolve
    await act(async () => {});

    expect(confirmIntake).toHaveBeenCalledTimes(1);
    const receipt = document.querySelector(".intake-receipt");
    expect(receipt).not.toBeNull();
    expect(receipt.textContent).toContain("My App");
    expect(receipt.textContent).toContain("Build login");
  });

  it("clicking Cancel before 5s prevents confirmIntake and removes banner", async () => {
    const { rerender } = await renderAndInit();

    await triggerComplete(rerender);

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(document.querySelector(".intake-countdown-banner")).not.toBeNull();

    await act(async () => {
      fireEvent.click(document.querySelector(".intake-countdown-banner button"));
    });

    expect(document.querySelector(".intake-countdown-banner")).toBeNull();
    expect(confirmIntake).not.toHaveBeenCalled();
  });

  it("cleans up timer on unmount without calling confirmIntake", async () => {
    const { rerender, unmount } = await renderAndInit();

    await triggerComplete(rerender);

    await act(async () => {
      vi.advanceTimersByTime(2000);
    });

    expect(document.querySelector(".intake-countdown-banner")).not.toBeNull();

    await act(async () => {
      unmount();
    });

    // Advance remaining time — no interval should fire
    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(confirmIntake).not.toHaveBeenCalled();
  });

  it("auto-navigates to board 3s after receipt renders", async () => {
    const { rerender } = await renderAndInit();

    await triggerComplete(rerender);

    await act(async () => {
      vi.advanceTimersByTime(5000);
    });
    await act(async () => {}); // let confirmIntake resolve

    expect(document.querySelector(".intake-receipt")).not.toBeNull();
    expect(onNavMock).not.toHaveBeenCalled(); // not yet — 3s hasn't elapsed

    await act(async () => {
      vi.advanceTimersByTime(3000);
    });

    expect(onNavMock).toHaveBeenCalledWith(42, "dashboard");
  });
});

describe("IntakeView manual create-project action", () => {
  beforeEach(() => {
    vi.mocked(listIntakeSessions).mockResolvedValue({ sessions: [] });
    vi.mocked(createIntakeSession).mockResolvedValue({
      session: { session_id: "test-session-1", updated_at: "2024-01-01T00:00:00+00:00" },
    });
    vi.mocked(confirmIntake).mockReset();
    currentStreamState = {
      streaming: false,
      reconnecting: false,
      text: "",
      toolEvents: [],
      error: null,
      result: null,
      usage: null,
      start: vi.fn(),
      stop: vi.fn(),
    };
  });

  async function setup() {
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    const utils = await act(async () =>
      render(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />)
    );
    await act(async () => {}); // let createIntakeSession resolve
    return utils;
  }

  async function setMissing(rerender, missing) {
    // The result-applying effect only fires on a streaming true→false
    // transition, so force one even if a prior call already left it false.
    currentStreamState = { ...currentStreamState, streaming: true, result: null };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
    currentStreamState = {
      ...currentStreamState,
      streaming: false,
      result: { text: "…", draft_spec: {}, missing, phase: null },
    };
    await act(async () => {
      rerender(<IntakeView onNavWorkspace={vi.fn()} onCancel={vi.fn()} />);
    });
  }

  it("renders the outstanding required fields from missing", async () => {
    const { rerender } = await setup();
    await setMissing(rerender, ["data_model", "stack"]);

    const req = document.querySelector(".intake-requirements");
    expect(req.textContent).toContain("data_model");
    expect(req.textContent).toContain("stack");
  });

  it("shows a plain message when nothing is missing", async () => {
    const { rerender } = await setup();
    await setMissing(rerender, []);

    const req = document.querySelector(".intake-requirements");
    expect(req.textContent).toContain("All required fields set");
  });

  it("disables Create project while fields are missing and enables it when none are", async () => {
    const { rerender } = await setup();
    await setMissing(rerender, ["data_model"]);

    expect(document.querySelector(".create-project-btn").disabled).toBe(true);

    await setMissing(rerender, []);

    expect(document.querySelector(".create-project-btn").disabled).toBe(false);
  });

  it("clicking Create project calls confirmIntake and renders the receipt", async () => {
    vi.mocked(confirmIntake).mockResolvedValue(CONFIRM_RESPONSE);
    const { rerender } = await setup();
    await setMissing(rerender, []);

    const btn = document.querySelector(".create-project-btn");
    await act(async () => {
      fireEvent.click(btn);
    });
    await act(async () => {});

    expect(confirmIntake).toHaveBeenCalledWith("test-session-1");
    const receipt = document.querySelector(".intake-receipt");
    expect(receipt).not.toBeNull();
    expect(receipt.textContent).toContain("My App");
  });

  it("renders the missing field names inline on a 422 response and re-enables the button", async () => {
    const err = new Error("spec is incomplete");
    err.status = 422;
    err.body = { error: "spec is incomplete", missing: ["data_model"] };
    vi.mocked(confirmIntake).mockRejectedValue(err);
    const { rerender } = await setup();
    await setMissing(rerender, []);

    const btn = document.querySelector(".create-project-btn");
    await act(async () => {
      fireEvent.click(btn);
    });
    await act(async () => {});

    expect(confirmIntake).toHaveBeenCalledWith("test-session-1");
    expect(document.querySelector(".intake-requirements").textContent).toContain("data_model");
    // Failure resets the guard so a corrected attempt can be retried
    expect(document.querySelector(".create-project-btn").disabled).toBe(false);
    // Receipt must not render on failure
    expect(document.querySelector(".intake-receipt")).toBeNull();
  });
});
