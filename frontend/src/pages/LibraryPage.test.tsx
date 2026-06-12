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
  RenderArtifact,
  RenderLabel,
  Track,
} from "../api";

const apiMock = vi.hoisted(() => ({
  health: vi.fn(),
  listPlugins: vi.fn(),
  listTracks: vi.fn(),
  listJobs: vi.fn(),
  getLabelRollup: vi.fn(),
  listProjects: vi.fn(),
  listRenders: vi.fn(),
  listRenderLabels: vi.fn(),
  renderCandidate: vi.fn(),
  cancelRender: vi.fn(),
  deleteRender: vi.fn(),
  addRenderLabel: vi.fn(),
  deleteRenderLabel: vi.fn(),
  renderAudioUrl: vi.fn(),
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
  schema_version: 7,
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

const renderArtifact: RenderArtifact = {
  id: 501,
  project_id: project.id,
  candidate_id: 11,
  from_track: "aaaaaaaaaaaaaaaa",
  to_track: "bbbbbbbbbbbbbbbb",
  technique: "long_crossfade",
  status: "completed",
  artifact_key: "projects/7/renders/render-501-11-long_crossfade.m4a",
  duration_sec: 12.4,
  sample_rate: 44100,
  channels: 2,
  claim_token: "token",
  request_config: {
    source_anchor_policy: "keep_outgoing_tempo",
    from_cue_sec: 10,
    to_cue_sec: 0,
    from_bpm: 124,
    to_bpm: 126,
    tempo_match_ratio: 0.9841,
    tempo_match_ratio_source: "candidate",
    transition_length_sec: 8,
    source_lead_in_sec: 12,
    target_tail_sec: 24,
    loudness_target_lufs: -14,
    output_sample_rate: 44100,
    output_channels: 2,
    confidence_snapshot: {
      from_tempo_confidence: 0.8,
      to_tempo_confidence: 0.8,
      from_key_confidence: null,
      to_key_confidence: null,
      from_beat_source: "librosa@0.1.0",
      to_beat_source: "librosa@0.1.0",
      from_key_source: null,
      to_key_source: null,
      from_beat_labels: ["correct"],
      to_beat_labels: ["correct"],
    },
  },
  actuals: {
    source_lufs: -13.2,
    target_lufs: -15.1,
    ffmpeg_version: "ffmpeg test",
    source_loudness: null,
    target_loudness: null,
    output_loudness: null,
    source_loudness_origin: "fresh",
    target_loudness_origin: "fresh",
  },
  warnings: ["beat-grid verification is unverified"],
  error: null,
  created_at: "2026-01-01 00:00:00",
  started_at: "2026-01-01 00:00:01",
  finished_at: "2026-01-01 00:00:02",
};

const goodRenderLabel: RenderLabel = {
  id: 71,
  render_id: renderArtifact.id,
  kind: "good",
  notes: "worked",
  created_at: "2026-01-01 00:00:03",
};

const resetApiMocks = () => {
  const methods = [
    apiMock.health,
    apiMock.listPlugins,
    apiMock.listTracks,
    apiMock.listJobs,
    apiMock.getLabelRollup,
    apiMock.listProjects,
    apiMock.listRenders,
    apiMock.listRenderLabels,
    apiMock.renderCandidate,
    apiMock.cancelRender,
    apiMock.deleteRender,
    apiMock.addRenderLabel,
    apiMock.deleteRenderLabel,
    apiMock.renderAudioUrl,
    apiMock.callPlugin,
    apiMock.ingestTrack,
    apiMock.createProject,
    apiMock.buildCandidateGraph,
  ];
  methods.forEach((method) => method.mockReset());
  apiMock.renderAudioUrl.mockImplementation((renderId: number) => `/api/renders/${renderId}/audio`);
};

function arrangeInitialLoad(
  overrides: {
    projects?: Project[];
    renders?: RenderArtifact[];
    labels?: Record<number, RenderLabel[]>;
  } = {},
) {
  apiMock.health.mockResolvedValue(health);
  apiMock.listPlugins.mockResolvedValue([] satisfies Plugin[]);
  apiMock.listTracks.mockResolvedValue([] satisfies Track[]);
  apiMock.listJobs.mockResolvedValue([] satisfies Job[]);
  apiMock.getLabelRollup.mockResolvedValue(emptyRollup);
  apiMock.listProjects.mockResolvedValue(overrides.projects ?? [project]);
  apiMock.listRenders.mockResolvedValue(overrides.renders ?? []);
  apiMock.listRenderLabels.mockImplementation((renderId: number) =>
    Promise.resolve(overrides.labels?.[renderId] ?? []),
  );
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
    expect(screen.getByText("v7")).toBeInTheDocument();
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
    expect(apiMock.listRenders).toHaveBeenCalledWith(project.id, expect.any(Object));
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
            tempo_match_ratio: 0.9841,
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
    apiMock.listRenders.mockResolvedValue([]);

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
    expect(screen.getByRole("button", { name: "render" })).toBeInTheDocument();
  });

  it("renders a graph candidate and shows the completed artifact", async () => {
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
          from_cue_bar: 8,
          to_cue_bar: 0,
          scores: {
            score: 0.91,
            tempo_delta_pct: 1.2,
            tempo_match_ratio: 0.9841,
            from_bpm: 124,
            to_bpm: 126,
            from_cue_sec: 10,
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
      warnings: [],
    };
    apiMock.buildCandidateGraph.mockResolvedValue(graph);
    apiMock.renderCandidate.mockResolvedValue(renderArtifact);
    apiMock.listRenders.mockResolvedValue([]);
    apiMock.listRenderLabels.mockResolvedValue([]);

    render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    await user.click(await screen.findByRole("button", { name: "build graph" }));
    await user.click(await screen.findByRole("button", { name: "render" }));

    await waitFor(() => {
      expect(apiMock.renderCandidate).toHaveBeenCalledWith(project.id, 11, {
        technique: "phrase_swap",
        force: false,
      });
    });
    expect(await screen.findByText("render #501")).toBeInTheDocument();
    expect(screen.getByText("beat-grid verification is unverified")).toBeInTheDocument();
  });

  it("adds and removes render labels from the render panel", async () => {
    arrangeInitialLoad({
      renders: [renderArtifact],
      labels: { [renderArtifact.id]: [goodRenderLabel] },
    });
    const newLabel: RenderLabel = {
      id: 72,
      render_id: renderArtifact.id,
      kind: "too_abrupt",
      notes: null,
      created_at: null,
    };
    apiMock.addRenderLabel.mockResolvedValue(newLabel);
    apiMock.deleteRenderLabel.mockResolvedValue(undefined);

    render(
      <MemoryRouter>
        <LibraryPage />
      </MemoryRouter>,
    );

    const user = userEvent.setup();
    expect(await screen.findByText("render #501")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "too abrupt" }));

    await waitFor(() => {
      expect(apiMock.addRenderLabel).toHaveBeenCalledWith(501, "too_abrupt");
    });
    expect(await screen.findByRole("button", { name: "too_abrupt x" })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "good x" }));
    expect(apiMock.deleteRenderLabel).toHaveBeenCalledWith(501, 71);
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
    expect(apiMock.listRenders).toHaveBeenCalledTimes(2);
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
