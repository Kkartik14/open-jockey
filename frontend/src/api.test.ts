import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type CandidateGraphBuildResult } from "./api";

function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

describe("api client contract", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    fetchMock.mockReset();
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it.each([
    {
      name: "health",
      call: () => api.health(),
      url: "/api/health",
    },
    {
      name: "listPlugins",
      call: () => api.listPlugins(),
      url: "/api/plugins",
    },
    {
      name: "callPlugin",
      call: () => api.callPlugin("echo", "ping", { hello: "world" }),
      url: "/api/plugins/echo/call",
      method: "POST",
      body: { method: "ping", params: { hello: "world" } },
    },
    {
      name: "ingestTrack",
      call: () => api.ingestTrack("/tmp/song.wav"),
      url: "/api/tracks/ingest",
      method: "POST",
      body: { path: "/tmp/song.wav" },
    },
    {
      name: "listTracks",
      call: () => api.listTracks(),
      url: "/api/tracks",
    },
    {
      name: "getTrack",
      call: () => api.getTrack("abc123"),
      url: "/api/tracks/abc123",
    },
    {
      name: "setTrackGenre",
      call: () => api.setTrackGenre("abc123", "Bollywood"),
      url: "/api/tracks/abc123",
      method: "PATCH",
      body: { genre: "Bollywood" },
    },
    {
      name: "analyzeTrack",
      call: () => api.analyzeTrack("abc123", "librosa", { force: true, timeout: 5 }),
      url: "/api/tracks/abc123/analyze/librosa",
      method: "POST",
      body: { force: true, timeout: 5 },
    },
    {
      name: "listAnalyses",
      call: () => api.listAnalyses("abc123"),
      url: "/api/tracks/abc123/analyses",
    },
    {
      name: "listLabels",
      call: () => api.listLabels(12),
      url: "/api/analyses/12/labels",
    },
    {
      name: "addLabel",
      call: () => api.addLabel(12, "correct", "locked"),
      url: "/api/analyses/12/labels",
      method: "POST",
      body: { kind: "correct", notes: "locked" },
    },
    {
      name: "enqueueJob",
      call: () => api.enqueueJob("demo", { x: 1 }),
      url: "/api/jobs",
      method: "POST",
      body: { kind: "demo", payload: { x: 1 } },
    },
    {
      name: "listJobs",
      call: () => api.listJobs(),
      url: "/api/jobs",
    },
    {
      name: "getLabelRollup",
      call: () => api.getLabelRollup(),
      url: "/api/labels/rollup",
    },
    {
      name: "getProfile",
      call: () => api.getProfile("abc123"),
      url: "/api/tracks/abc123/profile",
    },
    {
      name: "buildProfile",
      call: () => api.buildProfile("abc123"),
      url: "/api/tracks/abc123/profile/build",
      method: "POST",
    },
    {
      name: "getProfileCoverage",
      call: () => api.getProfileCoverage(),
      url: "/api/profiles/coverage",
    },
    {
      name: "createProject",
      call: () =>
        api.createProject({
          name: "Truth graph",
          intent: "smoke",
          plan: { target: "contract" },
        }),
      url: "/api/projects",
      method: "POST",
      body: {
        name: "Truth graph",
        intent: "smoke",
        plan: { target: "contract" },
      },
    },
    {
      name: "listProjects",
      call: () => api.listProjects(),
      url: "/api/projects",
    },
    {
      name: "getProject",
      call: () => api.getProject(7),
      url: "/api/projects/7",
    },
    {
      name: "listCandidates",
      call: () => api.listCandidates(7),
      url: "/api/projects/7/candidates",
    },
  ])("$name calls the locked backend route", async (testCase) => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));

    await testCase.call();

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(testCase.url);
    expect(init.method ?? "GET").toBe(testCase.method ?? "GET");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    if ("body" in testCase) {
      expect(JSON.parse(String(init.body))).toEqual(testCase.body);
    } else {
      expect(init.body).toBeUndefined();
    }
  });

  it("constructs the range-served audio URL without fetching", () => {
    expect(api.audioUrl("abc123")).toBe("/api/tracks/abc123/audio");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("posts candidate graph builds to the locked backend route with JSON body", async () => {
    const result: CandidateGraphBuildResult = {
      project: {
        id: 7,
        name: "Truth graph",
        intent: null,
        plan: null,
        render_artifact_key: null,
        created_at: null,
        updated_at: null,
      },
      requested_tracks: 2,
      usable_tracks: 2,
      skipped_tracks: {},
      candidates: [],
      warnings: [],
    };
    fetchMock.mockResolvedValueOnce(jsonResponse(result));

    await expect(
      api.buildCandidateGraph(7, {
        force: true,
        max_candidates_per_pair: 3,
      }),
    ).resolves.toEqual(result);

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/projects/7/candidates/build");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(String(init.body))).toEqual({
      force: true,
      max_candidates_per_pair: 3,
    });
  });

  it("returns no JSON for successful label deletes and throws on failed deletes", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(api.deleteLabel(4, 9)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/api/analyses/4/labels/9", {
      method: "DELETE",
      signal: undefined,
    });

    fetchMock.mockResolvedValueOnce(
      new Response("nope", { status: 500, statusText: "Internal Server Error" }),
    );
    await expect(api.deleteLabel(4, 9)).rejects.toThrow(
      "500 Internal Server Error",
    );
  });

  it("deletes projects through the backend 204 contract", async () => {
    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));

    await expect(api.deleteProject(7)).resolves.toBeUndefined();
    expect(fetchMock).toHaveBeenCalledWith("/api/projects/7", {
      method: "DELETE",
      signal: undefined,
    });

    fetchMock.mockResolvedValueOnce(new Response("missing", { status: 404, statusText: "Not Found" }));
    await expect(api.deleteProject(7)).rejects.toThrow("404 Not Found");
  });

  it("passes peak sample count and request timeout signal through to fetch", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ duration_sec: 3, samples: 4096, peaks: [0, 0.5, 1] }),
    );

    await api.getPeaks("abc123", 4096, { timeoutMs: 250 });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/api/tracks/abc123/peaks?samples=4096");
    expect(init.signal).toBeDefined();
  });

  it("surfaces backend error status and body text", async () => {
    fetchMock.mockResolvedValueOnce(
      new Response("backend down", {
        status: 503,
        statusText: "Service Unavailable",
      }),
    );

    await expect(api.health()).rejects.toThrow(
      "503 Service Unavailable: backend down",
    );
  });

  it("aborts fetch after timeoutMs elapses", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(init.signal?.reason ?? new Error("aborted"));
          });
        }),
    );

    const request = expect(api.health({ timeoutMs: 10 })).rejects.toThrow(
      "request timed out after 10ms",
    );

    await vi.advanceTimersByTimeAsync(10);
    await request;
  });

  it("composes caller abort signals with timeout signals", async () => {
    vi.useFakeTimers();
    const controller = new AbortController();
    fetchMock.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener("abort", () => {
            reject(init.signal?.reason ?? new Error("aborted"));
          });
        }),
    );

    const request = expect(
      api.health({
        signal: controller.signal,
        timeoutMs: 1_000,
      }),
    ).rejects.toThrow("caller cancelled");
    controller.abort(new Error("caller cancelled"));

    await request;
  });
});
