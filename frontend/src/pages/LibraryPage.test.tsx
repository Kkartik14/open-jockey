import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { act } from "react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type {
  CandidateGraphBuildResult,
  Health,
  Job,
  LabelRollup,
  Plugin,
  Project,
  Track,
} from "../api";

const apiMock = vi.hoisted(() => ({
  health: vi.fn(),
  listPlugins: vi.fn(),
  listTracks: vi.fn(),
  listJobs: vi.fn(),
  getLabelRollup: vi.fn(),
  listProjects: vi.fn(),
  callPlugin: vi.fn(),
  ingestTrack: vi.fn(),
  createProject: vi.fn(),
  buildCandidateGraph: vi.fn(),
}));

vi.mock("../api", () => ({
  api: apiMock,
}));

import { LibraryPage } from "./LibraryPage";

const health: Health = {
  status: "ok",
  version: "0.1.0",
  project_root: "/repo",
  store_root: "/repo/.aidj",
  schema_version: 6,
};

const emptyRollup: LabelRollup = {
  by_analyzer: {},
  by_analyzer_and_genre: {},
  total_labels: 0,
  total_labeled_runs: 0,
};

const project: Project = {
  id: 7,
  name: "Existing Graph",
  intent: "contract",
  plan: null,
  render_artifact_key: null,
  created_at: null,
  updated_at: null,
};

const resetApiMocks = () => {
  const methods = [
    apiMock.health,
    apiMock.listPlugins,
    apiMock.listTracks,
    apiMock.listJobs,
    apiMock.getLabelRollup,
    apiMock.listProjects,
    apiMock.callPlugin,
    apiMock.ingestTrack,
    apiMock.createProject,
    apiMock.buildCandidateGraph,
  ];
  methods.forEach((method) => method.mockReset());
};

function arrangeInitialLoad(overrides: { projects?: Project[] } = {}) {
  apiMock.health.mockResolvedValue(health);
  apiMock.listPlugins.mockResolvedValue([] satisfies Plugin[]);
  apiMock.listTracks.mockResolvedValue([] satisfies Track[]);
  apiMock.listJobs.mockResolvedValue([] satisfies Job[]);
  apiMock.getLabelRollup.mockResolvedValue(emptyRollup);
  apiMock.listProjects.mockResolvedValue(overrides.projects ?? [project]);
}

describe("LibraryPage contract wiring", () => {
  beforeEach(() => {
    resetApiMocks();
  });

  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  it("loads every library dashboard endpoint on mount", async () => {
    arrangeInitialLoad();

    render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    expect(await screen.findByText("0.1.0")).toBeInTheDocument();
    expect(screen.getByText("v6")).toBeInTheDocument();
    expect(screen.getByText("none discovered")).toBeInTheDocument();
    expect(
      screen.getByText(/no labels yet.*run analyzers.*tag each run/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText((_, node) => node?.textContent === "#7 Existing Graph"),
    ).toBeInTheDocument();

    expect(apiMock.health).toHaveBeenCalledTimes(1);
    expect(apiMock.listPlugins).toHaveBeenCalledTimes(1);
    expect(apiMock.listTracks).toHaveBeenCalledTimes(1);
    expect(apiMock.listJobs).toHaveBeenCalledTimes(1);
    expect(apiMock.getLabelRollup).toHaveBeenCalledTimes(1);
    expect(apiMock.listProjects).toHaveBeenCalledTimes(1);
  });

  it("builds the transition graph with the intended public API defaults", async () => {
    arrangeInitialLoad();
    const graph: CandidateGraphBuildResult = {
      project,
      requested_tracks: 2,
      usable_tracks: 2,
      skipped_tracks: {},
      candidates: [
        {
          id: 11,
          project_id: project.id,
          from_track: "aaaaaaaaaaaaaaaa",
          to_track: "bbbbbbbbbbbbbbbb",
          from_cue_bar: 0,
          to_cue_bar: 0,
          scores: {
            score: 0.91,
            tempo_delta_pct: 1.2,
            from_bpm: 124,
            to_bpm: 126,
            from_cue_sec: 0,
            to_cue_sec: 0,
            phrase_bars: 8,
            key_compatible: null,
            verification: "unverified",
            from_source: "librosa@0.1.0",
            to_source: "librosa@0.1.0",
            reasons: ["phrase_aligned", "unverified_sources"],
          },
          allowed_techniques: ["phrase_swap", "filter_blend", "long_crossfade"],
          created_at: null,
        },
      ],
      warnings: ["candidate graph is mechanical only"],
    };
    apiMock.buildCandidateGraph.mockResolvedValue(graph);

    render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "build graph" }));

    await waitFor(() => {
      expect(apiMock.buildCandidateGraph).toHaveBeenCalledWith(project.id, {
        force: true,
        max_candidates_per_pair: 3,
      });
    });
    expect(apiMock.createProject).not.toHaveBeenCalled();
    expect(await screen.findByText("requested 2")).toBeInTheDocument();
    expect(screen.getByText("usable 2")).toBeInTheDocument();
    expect(screen.getByText("candidates 1")).toBeInTheDocument();
    expect(screen.getByText("phrase_swap, filter_blend, long_crossfade")).toBeInTheDocument();
  });

  it("polls the dashboard every 5 seconds", async () => {
    vi.useFakeTimers();
    arrangeInitialLoad();

    render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.getByText("0.1.0")).toBeInTheDocument();
    expect(apiMock.health).toHaveBeenCalledTimes(1);

    await act(async () => {
      await vi.advanceTimersByTimeAsync(5_000);
    });
    expect(apiMock.health).toHaveBeenCalledTimes(2);
    expect(apiMock.listProjects).toHaveBeenCalledTimes(2);
  });

  it("aborts in-flight dashboard requests on unmount", async () => {
    let capturedSignal: AbortSignal | undefined;
    apiMock.health.mockImplementation((opts?: { signal?: AbortSignal }) => {
      capturedSignal = opts?.signal;
      return new Promise(() => {});
    });
    apiMock.listPlugins.mockResolvedValue([] satisfies Plugin[]);
    apiMock.listTracks.mockResolvedValue([] satisfies Track[]);
    apiMock.listJobs.mockResolvedValue([] satisfies Job[]);
    apiMock.getLabelRollup.mockResolvedValue(emptyRollup);
    apiMock.listProjects.mockResolvedValue([project]);

    const { unmount } = render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    await waitFor(() => {
      expect(capturedSignal).toBeDefined();
    });
    expect(capturedSignal?.aborted).toBe(false);

    unmount();

    expect(capturedSignal?.aborted).toBe(true);
  });
});
