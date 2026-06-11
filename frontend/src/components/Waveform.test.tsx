import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { Waveform, type BeatMark } from "./Waveform";

const apiMock = vi.hoisted(() => ({
  audioUrl: vi.fn((hash: string) => `/api/tracks/${hash}/audio`),
  getPeaks: vi.fn(),
}));

const waveSurferMock = vi.hoisted(() => {
  const handlers: Record<string, Array<() => void>> = {};
  let media: { paused: boolean } | null = null;
  const playPause = vi.fn(() => {
    if (media) media.paused = false;
    for (const handler of handlers.play ?? []) handler();
  });
  return {
    handlers,
    playPause,
    create: vi.fn((options: { media?: { paused: boolean } }) => {
      media = options.media ?? null;
      return {
        destroy: vi.fn(),
        on: vi.fn((event: string, handler: () => void) => {
          handlers[event] ??= [];
          handlers[event].push(handler);
          if (event === "ready") queueMicrotask(handler);
        }),
        playPause,
      };
    }),
  };
});

vi.mock("../api", () => ({
  api: apiMock,
}));

vi.mock("wavesurfer.js", () => ({
  default: waveSurferMock,
}));

class MockAudio extends EventTarget {
  currentTime = 0;
  error: MediaError | null = null;
  paused = true;
  preload = "";
  readyState = 2;
  src = "";

  pause = vi.fn();
}

describe("Waveform overlay contract", () => {
  beforeEach(() => {
    apiMock.audioUrl.mockClear();
    apiMock.getPeaks.mockReset();
    waveSurferMock.create.mockClear();
    waveSurferMock.playPause.mockClear();
    Object.keys(waveSurferMock.handlers).forEach((event) => {
      delete waveSurferMock.handlers[event];
    });
    vi.stubGlobal("Audio", MockAudio);
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it("aligns beat ticks and section bands to backend peak duration", async () => {
    apiMock.getPeaks.mockResolvedValue({
      duration_sec: 10,
      samples: 5,
      peaks: [0, 0.25, 0.5, 0.25, 0],
    });
    const beats = [
      { time_sec: 10, is_downbeat: true },
      { time_sec: 0, is_downbeat: true },
      { time_sec: 5, is_downbeat: false },
      { time_sec: -1, is_downbeat: false },
    ] as BeatMark[];

    const { container } = render(
      <Waveform
        trackHash="abc123"
        beats={beats}
        sections={[
          { start_sec: 0, end_sec: 5, label: "intro" },
          { start_sec: 5, end_sec: 10, label: "drop" },
        ]}
      />,
    );

    await waitFor(() => {
      expect(waveSurferMock.create).toHaveBeenCalled();
    });

    expect(apiMock.getPeaks).toHaveBeenCalledWith("abc123");
    expect(apiMock.audioUrl).toHaveBeenCalledWith("abc123");
    const lines = [...container.querySelectorAll("svg line")];
    expect(lines).toHaveLength(3);
    expect(lines.map((line) => line.getAttribute("x1"))).toEqual(["0", "50", "100"]);
    expect(lines.map((line) => line.getAttribute("y1"))).toEqual(["0", "60", "0"]);

    const intro = container.querySelector('[title^="intro"]') as HTMLElement;
    const drop = container.querySelector('[title^="drop"]') as HTMLElement;
    expect(intro).toHaveStyle({ left: "0%", width: "50%" });
    expect(drop).toHaveStyle({ left: "50%", width: "50%" });
  });

  it("schedules Web Audio click ticks aligned to visible beats", async () => {
    const oscillatorStart = vi.fn();
    const oscillatorStop = vi.fn();
    class MockAudioContext {
      currentTime = 10;
      destination = {};
      state = "running";

      close = vi.fn();
      resume = vi.fn();

      createGain() {
        return {
          connect: vi.fn(),
          gain: {
            exponentialRampToValueAtTime: vi.fn(),
            setValueAtTime: vi.fn(),
          },
        };
      }

      createOscillator() {
        return {
          connect: vi.fn((node: unknown) => node),
          frequency: { value: 0 },
          start: oscillatorStart,
          stop: oscillatorStop,
          type: "sine",
        };
      }
    }
    vi.stubGlobal("AudioContext", MockAudioContext);
    apiMock.getPeaks.mockResolvedValue({
      duration_sec: 1,
      samples: 4,
      peaks: [0, 0.5, 0.5, 0],
    });

    render(
      <Waveform
        trackHash="abc123"
        beats={[
          { time_sec: 0, is_downbeat: true },
          { time_sec: 0.1, is_downbeat: false },
          { time_sec: 0.5, is_downbeat: false },
        ]}
      />,
    );

    await waitFor(() => {
      expect(screen.getByRole("button", { name: "play" })).toBeEnabled();
    });
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("click track (3 beats)"));
    await user.click(screen.getByRole("button", { name: "play" }));

    await waitFor(() => {
      expect(oscillatorStart).toHaveBeenCalledTimes(2);
    });
    expect(oscillatorStart.mock.calls[0][0]).toBeCloseTo(10);
    expect(oscillatorStart.mock.calls[1][0]).toBeCloseTo(10.1);
    expect(oscillatorStop.mock.calls[0][0]).toBeCloseTo(10.06);
    expect(oscillatorStop.mock.calls[1][0]).toBeCloseTo(10.16);
  });
});
