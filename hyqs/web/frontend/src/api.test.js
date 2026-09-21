import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import {
  streamAI,
  streamJobDetail,
  getProjectsRuntime,
  getProjectRuntime,
  getJob,
  getJobUsage,
  getJobDependencyJobs,
  getUsage,
  getJobsFiledByBreakdown,
  getDeploymentReliability,
  getPageViewsSummary,
  getPerfTrend,
} from "./api.js";

function makeBody(events) {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const ev of events) {
        controller.enqueue(encoder.encode(`data: ${JSON.stringify(ev)}\n\n`));
      }
      controller.close();
    },
  });
}

function mockFetch(events, status = 200) {
  global.fetch = vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    body: makeBody(events),
    json: async () => ({ error: "err" }),
  });
}

beforeEach(() => {
  localStorage.setItem("hyqs_token", "test-tok");
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

describe("streamAI SSE parser", () => {
  it("fires onText for each text event in order", async () => {
    mockFetch([
      { type: "text", delta: "Hello" },
      { type: "text", delta: " world" },
      { type: "result", text: "Hello world" },
    ]);

    const deltas = [];
    let resolved;
    const done = new Promise((r) => {
      resolved = r;
    });

    streamAI(
      "/api/chat/stream",
      { text: "hi" },
      {
        onText: (d) => deltas.push(d),
        onResult: (r) => resolved(r),
      }
    );

    const r = await done;
    expect(deltas).toEqual(["Hello", " world"]);
    expect(r.text).toBe("Hello world");
  });

  it("fires onToolUse for tool_use events", async () => {
    mockFetch([
      { type: "tool_use", tool: "Read", input_summary: "Read foo.py" },
      { type: "result", text: "" },
    ]);

    const tools = [];
    let resolved;
    const done = new Promise((r) => {
      resolved = r;
    });

    streamAI(
      "/api/chat/stream",
      {},
      {
        onToolUse: (ev) => tools.push(ev),
        onResult: resolved,
      }
    );

    await done;
    expect(tools).toHaveLength(1);
    expect(tools[0].tool).toBe("Read");
  });

  it("fires onError for error events", async () => {
    mockFetch([{ type: "error", message: "rate limit reached" }]);

    let errMsg;
    let resolved;
    const done = new Promise((r) => {
      resolved = r;
    });

    streamAI(
      "/api/chat/stream",
      {},
      {
        onError: (err) => {
          errMsg = err.message;
          resolved();
        },
      }
    );

    await done;
    expect(errMsg).toBe("rate limit reached");
  });

  it("fires onThinking for thinking events", async () => {
    mockFetch([
      { type: "thinking", delta: "hmm..." },
      { type: "result", text: "" },
    ]);

    const thoughts = [];
    let resolved;
    const done = new Promise((r) => {
      resolved = r;
    });

    streamAI(
      "/api/chat/stream",
      {},
      {
        onThinking: (d) => thoughts.push(d),
        onResult: resolved,
      }
    );

    await done;
    expect(thoughts).toEqual(["hmm..."]);
  });

  it("cancel() aborts the stream without calling onError", async () => {
    // Stream that never closes
    let streamController;
    const body = new ReadableStream({
      start(c) {
        streamController = c;
      },
    });

    global.fetch = vi.fn().mockResolvedValue({ ok: true, status: 200, body });

    const onError = vi.fn();
    let abortCalled = false;
    const origAbort = AbortController.prototype.abort;
    AbortController.prototype.abort = function () {
      abortCalled = true;
      streamController?.close();
      return origAbort.call(this);
    };

    const { cancel } = streamAI("/api/chat/stream", {}, { onError });

    await new Promise((r) => setTimeout(r, 10));
    cancel();
    await new Promise((r) => setTimeout(r, 10));

    expect(abortCalled).toBe(true);
    expect(onError).not.toHaveBeenCalled();

    AbortController.prototype.abort = origAbort;
  });

  it("sends Authorization header with stored token", async () => {
    mockFetch([{ type: "result", text: "" }]);

    let resolved;
    const done = new Promise((r) => {
      resolved = r;
    });
    streamAI("/api/chat/stream", {}, { onResult: resolved });
    await done;

    expect(fetch).toHaveBeenCalledWith(
      "/api/chat/stream",
      expect.objectContaining({
        headers: expect.objectContaining({
          Authorization: "Bearer test-tok",
        }),
      })
    );
  });
});

describe("streamJobDetail", () => {
  class FakeEventSource {
    constructor(url) {
      this.url = url;
      this.listeners = {};
      this.close = vi.fn();
      FakeEventSource.instance = this;
    }

    addEventListener(name, callback) {
      this.listeners[name] = callback;
    }
  }

  beforeEach(() => {
    vi.stubGlobal("EventSource", FakeEventSource);
  });

  it("stays open while deploying then closes once on done without reporting expected EOF", () => {
    const onData = vi.fn();
    const onError = vi.fn();
    const stream = streamJobDetail(42, onData, onError);

    const deploying = { job: { id: 42, status: "deploying" }, events: [{ id: 1 }] };
    const done = { job: { id: 42, status: "done" }, events: [{ id: 1 }, { id: 2 }] };
    stream.listeners.job({
      data: JSON.stringify(deploying),
    });
    expect(onData).toHaveBeenLastCalledWith(deploying);
    expect(stream.close).not.toHaveBeenCalled();

    stream.listeners.job({ data: JSON.stringify(done) });
    stream.onerror(new Event("error"));

    expect(onData.mock.calls.map(([snapshot]) => snapshot)).toEqual([deploying, done]);
    expect(stream.close).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });

  it("passes current_executor through job snapshots without transformation", () => {
    const onData = vi.fn();
    const stream = streamJobDetail(42, onData, vi.fn());
    const snapshot = {
      job: {
        id: 42,
        status: "running",
        current_executor: { kind: "agent", label: "codex", agent_id: 8, provider: "codex" },
      },
      events: [{ id: 1 }],
    };

    stream.listeners.job({ data: JSON.stringify(snapshot) });

    expect(onData).toHaveBeenCalledWith(snapshot);
  });

  it.each(["done", "failed", "cancelled"])("closes on terminal %s snapshots", (status) => {
    const onData = vi.fn();
    const onError = vi.fn();
    const stream = streamJobDetail(42, onData, onError);
    const snapshot = { job: { id: 42, status }, events: [] };

    stream.listeners.job({ data: JSON.stringify(snapshot) });
    stream.onerror(new Event("error"));

    expect(onData).toHaveBeenCalledWith(snapshot);
    expect(stream.close).toHaveBeenCalledOnce();
    expect(onError).not.toHaveBeenCalled();
  });

  it("reports a stream error before a terminal snapshot arrives", () => {
    const onError = vi.fn();
    const stream = streamJobDetail(42, vi.fn(), onError);

    const error = new Event("error");
    stream.onerror(error);

    expect(onError).toHaveBeenCalledWith(error);
  });
});

describe("runtime status API clients", () => {
  function mockJsonFetch(body) {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => body,
    });
  }

  it("getProjectsRuntime fetches GET /api/projects/runtime", async () => {
    mockJsonFetch({ projects: [] });
    await getProjectsRuntime();
    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/runtime",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("getProjectRuntime fetches GET /api/projects/{id}/runtime", async () => {
    mockJsonFetch({ deploy_mode: "single", containers: [] });
    await getProjectRuntime(42);
    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/42/runtime",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("getJob fetches GET /api/jobs/{id} and resolves to response.job", async () => {
    const job = { id: 7, status: "done" };
    mockJsonFetch({ job });
    const result = await getJob(7);
    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/7",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(job);
  });
});

describe("getJob", () => {
  it("fetches GET /api/jobs/{id} and unwraps the job", async () => {
    const job = {
      id: 692,
      idea: "x",
      current_executor: { kind: "agent", label: "claude", agent_id: 3, provider: "claude" },
    };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ job }),
    });

    const result = await getJob(692);

    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/692",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(job);
  });
});

describe("getJobUsage", () => {
  it("fetches GET /api/jobs/{id}/usage and resolves to the raw array", async () => {
    const usage = [{ source: "build", model: "claude-sonnet-4-6", tokens: 100 }];
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => usage,
    });

    const result = await getJobUsage(692);

    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/692/usage",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(usage);
  });
});

describe("getJobDependencyJobs", () => {
  it("resolves dependency ids to full job objects via getJob", async () => {
    const depIds = [10, 11];
    const jobs = {
      10: { id: 10, idea: "dep one" },
      11: { id: 11, idea: "dep two" },
    };
    global.fetch = vi.fn((url) => {
      if (url === "/api/jobs/692/dependencies") {
        return Promise.resolve({
          ok: true,
          status: 200,
          json: async () => ({ depends_on: depIds }),
        });
      }
      const id = Number(url.split("/").pop());
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({ job: jobs[id] }),
      });
    });

    const result = await getJobDependencyJobs(692);

    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/692/dependencies",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/10",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/11",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual([jobs[10], jobs[11]]);
  });
});

describe("getUsage", () => {
  function mockJsonFetch(body) {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => body,
    });
  }

  it("fetches GET /api/usage when neither since nor projectId is given", async () => {
    mockJsonFetch({});
    await getUsage();
    expect(fetch).toHaveBeenCalledWith(
      "/api/usage",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("fetches GET /api/usage?since=... when only since is given", async () => {
    mockJsonFetch({});
    await getUsage("2026-07-01");
    expect(fetch).toHaveBeenCalledWith(
      "/api/usage?since=2026-07-01",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("fetches GET /api/usage?project_id=... when only projectId is given", async () => {
    mockJsonFetch({});
    await getUsage(undefined, 42);
    expect(fetch).toHaveBeenCalledWith(
      "/api/usage?project_id=42",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("fetches GET /api/usage?since=...&project_id=... when both are given", async () => {
    mockJsonFetch({});
    await getUsage("2026-07-01", 42);
    expect(fetch).toHaveBeenCalledWith(
      "/api/usage?since=2026-07-01&project_id=42",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });
});

describe("getJobsFiledByBreakdown", () => {
  it("fetches GET /api/jobs/filed-by-breakdown when since is omitted", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ breakdown: [] }),
    });

    const result = await getJobsFiledByBreakdown();

    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/filed-by-breakdown",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual([]);
  });

  it("fetches GET /api/jobs/filed-by-breakdown?since=... when since is given", async () => {
    const breakdown = [{ source: "slack", source_actor: "alice", count: 3 }];
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ breakdown }),
    });

    const result = await getJobsFiledByBreakdown("2026-07-01");

    expect(fetch).toHaveBeenCalledWith(
      "/api/jobs/filed-by-breakdown?since=2026-07-01",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(breakdown);
  });
});

describe("getDeploymentReliability", () => {
  it("fetches GET /api/deployment-reliability when since is omitted", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ breakdown: [] }),
    });

    const result = await getDeploymentReliability();

    expect(fetch).toHaveBeenCalledWith(
      "/api/deployment-reliability",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual([]);
  });

  it("fetches GET /api/deployment-reliability?since=... when since is given", async () => {
    const breakdown = [{ environment_id: 1, environment_name: "prod", success_count: 5 }];
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ breakdown }),
    });

    const result = await getDeploymentReliability("2026-07-01");

    expect(fetch).toHaveBeenCalledWith(
      "/api/deployment-reliability?since=2026-07-01",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(breakdown);
  });
});

describe("getPageViewsSummary", () => {
  it("fetches the bare endpoint and returns the summary object directly", async () => {
    const summary = { total: 12, unique_visitors: 7, paths: [] };
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => summary,
    });

    const result = await getPageViewsSummary();

    expect(fetch).toHaveBeenCalledWith(
      "/api/admin/page-views",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
    expect(result).toEqual(summary);
  });

  it.each([
    [{ since: "2026-08-01" }, "/api/admin/page-views?since=2026-08-01"],
    [{ until: "2026-09-01" }, "/api/admin/page-views?until=2026-09-01"],
    [{ path: "/how it works" }, "/api/admin/page-views?path=%2Fhow+it+works"],
  ])("includes only the supplied query parameter", async (filters, expectedUrl) => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({}),
    });

    await getPageViewsSummary(filters);

    expect(fetch).toHaveBeenCalledWith(expectedUrl, expect.any(Object));
  });

  it.each([
    [
      { since: "2026-08-01T00:00:00Z", until: "2026-09-01T00:00:00Z", path: "/docs?a=1" },
      "/api/admin/page-views?since=2026-08-01T00%3A00%3A00Z&until=2026-09-01T00%3A00%3A00Z&path=%2Fdocs%3Fa%3D1",
    ],
    [
      { since: "2026-08-01T00:00:00Z", until: "", path: "/docs?a=1" },
      "/api/admin/page-views?since=2026-08-01T00%3A00%3A00Z&path=%2Fdocs%3Fa%3D1",
    ],
    [{ since: "", until: undefined, path: "" }, "/api/admin/page-views"],
  ])(
    "appends supplied parameters in order and omits absent or empty values",
    async (filters, expectedUrl) => {
      global.fetch = vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        json: async () => ({}),
      });

      await getPageViewsSummary(filters);

      expect(fetch).toHaveBeenCalledWith(expectedUrl, expect.any(Object));
    }
  );

  it("propagates forbidden errors from the shared API helper", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: false,
      status: 403,
      json: async () => ({ error: "forbidden" }),
    });

    await expect(getPageViewsSummary()).rejects.toEqual(new Error("forbidden"));
  });
});

describe("getPerfTrend", () => {
  it("fetches GET /api/projects/{id}/performance/trend with no filters", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ points: [] }),
    });

    await getPerfTrend(7);

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/7/performance/trend",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });

  it("builds the query string from filters like the other perf getters", async () => {
    global.fetch = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: async () => ({ points: [] }),
    });

    await getPerfTrend(7, { from: "2026-06-01", to: "2026-07-01", stage: "build", epic_id: 3 });

    expect(fetch).toHaveBeenCalledWith(
      "/api/projects/7/performance/trend?from=2026-06-01&to=2026-07-01&stage=build&epic_id=3",
      expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer test-tok" }),
      })
    );
  });
});
