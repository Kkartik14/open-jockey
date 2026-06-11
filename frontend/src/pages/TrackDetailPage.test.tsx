import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useNavigate } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AnalysisRun, Plugin, Track } from "../api";

const apiMock = vi.hoisted(() => ({
  getTrack: vi.fn(),
  listPlugins: vi.fn(),
  listAnalyses: vi.fn(),
  setTrackGenre: vi.fn(),
  analyzeTrack: vi.fn(),
  addLabel: vi.fn(),
  deleteLabel: vi.fn(),
}));

vi.mock("../api", () => ({
  api: apiMock,
}));

vi.mock("../components/Waveform", async () => {
  const React = await import("react");
  return {
    Waveform: ({
      trackHash,
      beats,
      sections,
    }: {
      trackHash: string;
      beats?: unknown[];
      sections?: unknown[];
    }) =>
      React.createElement("div", {
        "data-beats": beats?.length ?? 0,
        "data-sections": sections?.length ?? 0,
        "data-testid": "waveform",
        "data-track-hash": trackHash,
      }),
  };
});

import { TrackDetailPage } from "./TrackDetailPage";

const track: Track = {
  content_hash: "abc123",
  source_path: "/music/song.mp3",
  duration_sec: 12,
  sample_rate: 44_100,
  channels: 2,
  format: "mp3",
  bitrate: null,
  file_size: 2048,
  genre: "House",
};

const plugins: Plugin[] = [
  {
    name: "librosa",
    version: "0.1.0",
    description: "local beat grid",
    python: ">=3.11",
    hardware: { cpu_cores: 1, ram_mb: 512, gpu: "none" },
    concurrency_safe: true,
    default_timeout_sec: 60,
    cloud_audio: false,
  },
];

const cloudPlugins: Plugin[] = [
  ...plugins,
  {
    name: "allin1_remote",
    version: "0.1.0",
    description: "remote analyzer",
    python: ">=3.11",
    hardware: { cpu_cores: 1, ram_mb: 512, gpu: "none" },
    concurrency_safe: true,
    default_timeout_sec: 600,
    cloud_audio: true,
  },
];

const completedRun: AnalysisRun = {
  id: 99,
  track_hash: "abc123",
  analyzer_name: "librosa",
  analyzer_version: "0.1.0",
  status: "completed",
  output: {
    tempo: { bpm: 124, confidence: 0.8 },
    beats: [
      { time_sec: 0, is_downbeat: true },
      { time_sec: 0.5, is_downbeat: false },
    ],
    sections: [{ start_sec: 0, end_sec: 4, label: " intro " }],
    duration_sec: 12,
  },
  confidence: 0.8,
  error: null,
  started_at: "2026-01-01 00:00:00",
  finished_at: "2026-01-01 00:00:01",
  labels: [],
};

const nextTrack: Track = {
  ...track,
  content_hash: "def456",
  source_path: "/music/next.mp3",
  genre: "DnB",
};

function resetApiMocks() {
  [
    apiMock.getTrack,
    apiMock.listPlugins,
    apiMock.listAnalyses,
    apiMock.setTrackGenre,
    apiMock.analyzeTrack,
    apiMock.addLabel,
    apiMock.deleteLabel,
  ].forEach((mock) => mock.mockReset());
}

function arrangeTrackDetail() {
  apiMock.getTrack.mockResolvedValue(track);
  apiMock.listPlugins.mockResolvedValue(plugins);
  apiMock.listAnalyses.mockResolvedValue([completedRun]);
  apiMock.setTrackGenre.mockResolvedValue({ ...track, genre: "Bollywood" });
  apiMock.analyzeTrack.mockResolvedValue({ ...completedRun, id: 100 });
  apiMock.addLabel.mockResolvedValue({
    id: 1,
    analysis_run_id: completedRun.id,
    kind: "correct",
    notes: null,
    created_at: "2026-01-01 00:00:02",
  });
  apiMock.deleteLabel.mockResolvedValue(undefined);
}

function renderTrackDetail() {
  return render(
    <MemoryRouter initialEntries={["/track/abc123"]}>
      <Routes>
        <Route path="/track/:hash" element={<TrackDetailPage />} />
      </Routes>
    </MemoryRouter>,
  );
}

function NavigateToNextTrack() {
  const navigate = useNavigate();
  return (
    <button type="button" onClick={() => navigate("/track/def456")}>
      next track
    </button>
  );
}

describe("TrackDetailPage contract wiring", () => {
  beforeEach(() => {
    resetApiMocks();
  });

  afterEach(() => {
    cleanup();
  });

  it("loads track detail data and preserves the accessible control contract", async () => {
    arrangeTrackDetail();

    renderTrackDetail();

    expect(await screen.findByText("song.mp3")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Track genre")).toHaveValue("House");
    });
    expect(
      screen.getByLabelText("Choose analyzer to run on this track"),
    ).toBeInTheDocument();
    expect(screen.getByTestId("waveform")).toHaveAttribute(
      "data-track-hash",
      "abc123",
    );
    expect(screen.getByTestId("waveform")).toHaveAttribute("data-beats", "2");
  });

  it("wires analyzer runs, genre saves, and label creation to the API", async () => {
    arrangeTrackDetail();
    const user = userEvent.setup();

    renderTrackDetail();

    await screen.findByText("song.mp3");

    await user.selectOptions(
      screen.getByLabelText("Choose analyzer to run on this track"),
      "librosa",
    );
    await user.click(screen.getByRole("button", { name: "run" }));
    await waitFor(() => {
      expect(apiMock.analyzeTrack).toHaveBeenCalledWith("abc123", "librosa", {
        force: false,
      });
    });

    const genreInput = screen.getByLabelText("Track genre");
    await user.clear(genreInput);
    await user.type(genreInput, "Bollywood");
    await user.click(screen.getByRole("button", { name: "save" }));
    await waitFor(() => {
      expect(apiMock.setTrackGenre).toHaveBeenCalledWith("abc123", "Bollywood");
    });

    await user.click(screen.getByTitle("mark as correct"));
    await waitFor(() => {
      expect(apiMock.addLabel).toHaveBeenCalledWith(completedRun.id, "correct");
    });
  });

  it("removes the most recent label when a present label pill is clicked", async () => {
    arrangeTrackDetail();
    apiMock.listAnalyses.mockResolvedValue([
      {
        ...completedRun,
        labels: [
          {
            id: 5,
            analysis_run_id: completedRun.id,
            kind: "correct",
            notes: null,
            created_at: "2026-01-01 00:00:02",
          },
        ],
      },
    ]);
    const user = userEvent.setup();

    renderTrackDetail();

    await user.click(await screen.findByTitle(/correct \(1\).*remove the most recent/));

    await waitFor(() => {
      expect(apiMock.deleteLabel).toHaveBeenCalledWith(completedRun.id, 5);
    });
  });

  it("shows the cloud-audio warning when a cloud analyzer is selected", async () => {
    arrangeTrackDetail();
    apiMock.listPlugins.mockResolvedValue(cloudPlugins);
    const user = userEvent.setup();

    renderTrackDetail();

    await screen.findByText("song.mp3");
    await user.selectOptions(
      screen.getByLabelText("Choose analyzer to run on this track"),
      "allin1_remote",
    );

    expect(screen.getByText(/uploads audio off-machine/i)).toBeInTheDocument();
    expect(screen.getByText("AIDJ_ALLOW_CLOUD_AUDIO=1")).toBeInTheDocument();
  });

  it("resets route-local state when navigating to a different track hash", async () => {
    apiMock.getTrack.mockImplementation((hash: string) =>
      Promise.resolve(hash === "abc123" ? track : nextTrack),
    );
    apiMock.listPlugins.mockResolvedValue(plugins);
    apiMock.listAnalyses.mockImplementation((hash: string) =>
      Promise.resolve(hash === "abc123" ? [completedRun] : []),
    );
    const user = userEvent.setup();

    render(
      <MemoryRouter initialEntries={["/track/abc123"]}>
        <NavigateToNextTrack />
        <Routes>
          <Route path="/track/:hash" element={<TrackDetailPage />} />
        </Routes>
      </MemoryRouter>,
    );

    await screen.findByText("song.mp3");
    await user.selectOptions(
      screen.getByLabelText("Choose analyzer to run on this track"),
      "librosa",
    );
    expect(screen.getByLabelText("Choose analyzer to run on this track")).toHaveValue(
      "librosa",
    );

    await user.click(screen.getByRole("button", { name: "next track" }));

    expect(await screen.findByText("next.mp3")).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByLabelText("Track genre")).toHaveValue("DnB");
    });
    expect(screen.getByLabelText("Choose analyzer to run on this track")).toHaveValue(
      "",
    );
    expect(screen.getByTestId("waveform")).toHaveAttribute(
      "data-track-hash",
      "def456",
    );
  });
});
