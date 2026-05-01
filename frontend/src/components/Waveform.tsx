/**
 * Waveform display backed by precomputed peaks.
 *
 * The component fetches ``/api/tracks/{hash}/peaks`` for the visual, and
 * hands WaveSurfer an HTMLAudioElement pointing at ``/api/tracks/{hash}/audio``
 * for playback. We do not pass ``url`` to WaveSurfer — that would make it
 * fetch + decode the entire blob to draw the waveform itself, which negates
 * the backend's Range support and falls over on long lossless tracks.
 *
 * Layout: padding lives on the *outer* container, and the WaveSurfer canvas
 * + SVG overlay + section bands all share the inner content box. That keeps
 * the beat-tick x-coordinates aligned with the waveform's actual samples.
 *
 * Click track: AudioContext is created synchronously inside the user's
 * checkbox click (and resumed on each play) so browsers don't leave it
 * suspended. Beats are validated + sorted defensively before scheduling so a
 * malformed plugin output can't break the metronome.
 */
import { useEffect, useMemo, useReducer, useRef } from "react";
import WaveSurfer from "wavesurfer.js";
import { api, type Peaks } from "../api";

export interface BeatMark {
  time_sec: number;
  is_downbeat: boolean;
}

export interface SectionMark {
  start_sec: number;
  end_sec: number;
  label: string;
}

interface WaveformProps {
  trackHash: string;
  beats?: BeatMark[];
  sections?: SectionMark[];
}

const SECTION_COLOURS: Record<string, string> = {
  intro: "bg-blue-700/50",
  verse: "bg-emerald-700/50",
  chorus: "bg-fuchsia-700/60",
  bridge: "bg-amber-700/50",
  drop: "bg-red-700/60",
  breakdown: "bg-cyan-700/50",
  instrumental: "bg-indigo-700/50",
  outro: "bg-zinc-600/50",
  unknown: "bg-zinc-700/40",
};

type WaveformState = {
  peaks: Peaks | null;
  peaksError: string | null;
  ready: boolean;
  playing: boolean;
  clickEnabled: boolean;
  playbackError: string | null;
};

const INITIAL_WAVEFORM_STATE: WaveformState = {
  peaks: null,
  peaksError: null,
  ready: false,
  playing: false,
  clickEnabled: false,
  playbackError: null,
};

function waveformReducer(
  state: WaveformState,
  patch: Partial<WaveformState>,
): WaveformState {
  return { ...state, ...patch };
}

export function Waveform({ trackHash, beats, sections }: WaveformProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // AudioContext for the click track. Created lazily on user gesture (Med5).
  const clickCtxRef = useRef<AudioContext | null>(null);

  const [state, dispatchWaveform] = useReducer(
    waveformReducer,
    INITIAL_WAVEFORM_STATE,
  );
  const { peaks, peaksError, ready, playing, clickEnabled, playbackError } = state;

  // Defensive normalisation — drop garbage and sort by time. Plugins are
  // supposed to return well-formed beat arrays, but we don't trust them.
  const safeBeats = useMemo(() => {
    if (!beats) return [];
    return beats
      .filter(
        (b): b is BeatMark =>
          !!b &&
          typeof b.time_sec === "number" &&
          Number.isFinite(b.time_sec) &&
          b.time_sec >= 0 &&
          typeof b.is_downbeat === "boolean",
      )
      .slice()
      .sort((a, b) => a.time_sec - b.time_sec);
  }, [beats]);

  const safeSections = useMemo(() => {
    if (!sections) return [];
    const parsed: SectionMark[] = [];
    for (const s of sections) {
      if (
        !s ||
        typeof s.start_sec !== "number" ||
        typeof s.end_sec !== "number" ||
        !Number.isFinite(s.start_sec) ||
        !Number.isFinite(s.end_sec) ||
        s.start_sec < 0 ||
        s.end_sec <= s.start_sec ||
        typeof s.label !== "string"
      ) {
        continue;
      }
      const label = s.label.trim();
      if (label) parsed.push({ ...s, label });
    }
    return parsed.sort((a, b) => a.start_sec - b.start_sec);
  }, [sections]);

  // Fetch peaks before mounting the player.
  useEffect(() => {
    let active = true;
    dispatchWaveform({
      peaks: null,
      peaksError: null,
      ready: false,
      playing: false,
      clickEnabled: false,
      playbackError: null,
    });
    api
      .getPeaks(trackHash)
      .then((p) => {
        if (active) dispatchWaveform({ peaks: p });
      })
      .catch((e: Error) => {
        if (active) dispatchWaveform({ peaksError: e.message });
      });
    return () => {
      active = false;
    };
  }, [trackHash]);

  // Spin up WaveSurfer once we have peaks.
  useEffect(() => {
    if (!peaks || !containerRef.current) return;

    const audio = new Audio();
    audio.src = api.audioUrl(trackHash);
    audio.preload = "metadata";

    function handleAudioError() {
      const code = audio.error?.code ?? 0;
      const messages: Record<number, string> = {
        1: "audio fetch aborted",
        2: "audio network error",
        3: "audio decode error",
        4: "audio source not supported",
      };
      dispatchWaveform({
        playing: false,
        playbackError: messages[code] ?? `audio error (code ${code})`,
      });
    }
    audio.addEventListener("error", handleAudioError);
    audioRef.current = audio;

    const ws = WaveSurfer.create({
      container: containerRef.current,
      waveColor: "#52525b",
      progressColor: "#a855f7",
      cursorColor: "#a855f7",
      height: 96,
      barWidth: 2,
      barRadius: 1,
      barGap: 1,
      media: audio,
      peaks: [peaks.peaks],
      duration: peaks.duration_sec,
    });
    wsRef.current = ws;

    ws.on("ready", () => dispatchWaveform({ ready: true }));
    ws.on("play", () => dispatchWaveform({ playing: true }));
    ws.on("pause", () => dispatchWaveform({ playing: false }));
    ws.on("finish", () => dispatchWaveform({ playing: false }));
    ws.on("error", (err: unknown) => {
      const message = err instanceof Error ? err.message : String(err);
      dispatchWaveform({ playing: false, playbackError: message });
    });

    if (audio.readyState >= 2) dispatchWaveform({ ready: true });

    return () => {
      ws.destroy();
      wsRef.current = null;
      audio.removeEventListener("error", handleAudioError);
      audio.pause();
      audio.src = "";
      audioRef.current = null;
    };
  }, [peaks, trackHash]);

  // Close the AudioContext on unmount. We never close mid-life because
  // AudioContext is unique-per-document and reopening costs perf.
  useEffect(
    () => () => {
      if (clickCtxRef.current) {
        void clickCtxRef.current.close();
        clickCtxRef.current = null;
      }
    },
    [],
  );

  useClickTrack({
    audio: audioRef.current,
    beats: safeBeats,
    enabled: clickEnabled && playing,
    ctxRef: clickCtxRef,
  });

  /** Ensures the AudioContext exists + is running. Called from user-gesture
   * handlers (checkbox + play button) so browsers don't leave it suspended. */
  function ensureClickCtx(): AudioContext {
    if (!clickCtxRef.current) {
      clickCtxRef.current = new AudioContext();
    }
    if (clickCtxRef.current.state === "suspended") {
      void clickCtxRef.current.resume();
    }
    return clickCtxRef.current;
  }

  function onToggleClickTrack(checked: boolean) {
    if (checked) ensureClickCtx();
    dispatchWaveform({ clickEnabled: checked });
  }

  function togglePlay() {
    // If click-track is on, browsers may have suspended the context during
    // a long pause; resume from this gesture.
    if (clickEnabled && clickCtxRef.current) ensureClickCtx();
    void wsRef.current?.playPause();
  }

  if (peaksError) {
    return <FallbackAudio trackHash={trackHash} note={peaksError} />;
  }

  const duration = peaks?.duration_sec ?? 0;
  const showOverlay = duration > 0 && safeBeats.length > 0;
  const showSections = duration > 0 && safeSections.length > 0;

  return (
    <div className="space-y-2">
      {/* Outer container holds padding + background. Inner relative div is
          unpadded so WaveSurfer's canvas, the SVG overlay, and the section
          bands all share the same x-axis coordinate space. */}
      <div className="space-y-1 rounded bg-zinc-900/50 px-2 py-3">
        <div className="relative">
          <div ref={containerRef} />
          {showOverlay && (
            <BeatGridOverlay beats={safeBeats} duration={duration} />
          )}
        </div>
        {showSections && (
          <SectionBands sections={safeSections} duration={duration} />
        )}
      </div>
      <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-400">
        <button
          type="button"
          onClick={togglePlay}
          disabled={!peaks || !ready || playbackError !== null}
          className="rounded bg-zinc-700 px-3 py-1 text-zinc-100 hover:bg-zinc-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {playing ? "pause" : "play"}
        </button>
        {safeBeats.length > 0 && (
          <label className="flex cursor-pointer items-center gap-1.5 text-zinc-400">
            <input
              type="checkbox"
              checked={clickEnabled}
              onChange={(e) => onToggleClickTrack(e.target.checked)}
              className="accent-purple-500"
            />
            click track ({safeBeats.length} beats)
          </label>
        )}
        {!peaks && <span>computing peaks…</span>}
        {peaks && !ready && !playbackError && <span>loading audio…</span>}
        {peaks && (
          <span>
            {peaks.duration_sec.toFixed(1)}s · {peaks.samples} peak buckets
          </span>
        )}
        {playbackError && (
          <span className="text-red-300">playback: {playbackError}</span>
        )}
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Beat-grid overlay — absolute SVG sized to the waveform's content box
// ---------------------------------------------------------------------------

function BeatGridOverlay({
  beats,
  duration,
}: {
  beats: BeatMark[];
  duration: number;
}) {
  return (
    <svg
      className="pointer-events-none absolute inset-0 h-full w-full"
      preserveAspectRatio="none"
      viewBox="0 0 100 100"
    >
      {beats.map((b) => {
        const x = (b.time_sec / duration) * 100;
        const y1 = b.is_downbeat ? 0 : 60;
        const stroke = b.is_downbeat ? "#fbbf24" : "#a855f7";
        const opacity = b.is_downbeat ? 0.85 : 0.45;
        return (
          <line
            key={`${b.time_sec.toFixed(6)}-${b.is_downbeat ? "d" : "b"}`}
            x1={x}
            x2={x}
            y1={y1}
            y2={100}
            stroke={stroke}
            strokeWidth={b.is_downbeat ? 0.4 : 0.2}
            opacity={opacity}
            vectorEffect="non-scaling-stroke"
          />
        );
      })}
    </svg>
  );
}

// ---------------------------------------------------------------------------
// Section bands — horizontal coloured spans aligned to the waveform width
// ---------------------------------------------------------------------------

function SectionBands({
  sections,
  duration,
}: {
  sections: SectionMark[];
  duration: number;
}) {
  return (
    <div className="relative h-4 w-full overflow-hidden rounded">
      {sections.map((s) => {
        const left = (s.start_sec / duration) * 100;
        const width = ((s.end_sec - s.start_sec) / duration) * 100;
        const colour = SECTION_COLOURS[s.label] ?? SECTION_COLOURS.unknown;
        return (
          <div
            key={`${s.start_sec.toFixed(3)}-${s.end_sec.toFixed(3)}-${s.label}`}
            className={`absolute top-0 h-full ${colour}`}
            style={{ left: `${left}%`, width: `${width}%` }}
            title={`${s.label} · ${s.start_sec.toFixed(1)}s–${s.end_sec.toFixed(1)}s`}
          >
            <span className="block truncate px-1 text-[9px] font-mono uppercase tracking-wider text-zinc-100/80">
              {s.label}
            </span>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Click-track scheduler — Web Audio metronome aligned to audio.currentTime.
// AudioContext lives on the parent's ref so it can be created from a real
// user gesture (Med5).
// ---------------------------------------------------------------------------

function useClickTrack({
  audio,
  beats,
  enabled,
  ctxRef,
}: {
  audio: HTMLAudioElement | null;
  beats: BeatMark[];
  enabled: boolean;
  ctxRef: React.RefObject<AudioContext | null>;
}) {
  useEffect(() => {
    if (!audio || !enabled || beats.length === 0) return;
    const ctx = ctxRef.current;
    if (!ctx) return; // user hasn't enabled click track yet (no gesture)

    const queued = new Set<number>();
    const lookaheadSec = 0.15;
    const lookbehindSec = 0.15;
    const pollMs = 50;

    function schedule() {
      if (audio.paused) return;
      const audioNow = audio.currentTime;
      const ctxNow = ctx!.currentTime;
      // beats are time-ordered (parent sorts) so we can break early.
      for (const b of beats) {
        if (b.time_sec < audioNow - lookbehindSec) continue;
        if (b.time_sec > audioNow + lookaheadSec) break;
        if (queued.has(b.time_sec)) continue;
        const when = Math.max(ctxNow, ctxNow + (b.time_sec - audioNow));
        playClick(ctx!, when, b.is_downbeat);
        queued.add(b.time_sec);
      }
      for (const t of queued) {
        if (t < audioNow - 1) queued.delete(t);
      }
    }

    function onSeek() {
      queued.clear();
      schedule();
    }
    audio.addEventListener("seeking", onSeek);

    schedule();
    const timer = setInterval(schedule, pollMs);
    return () => {
      clearInterval(timer);
      audio.removeEventListener("seeking", onSeek);
    };
  }, [audio, beats, enabled, ctxRef]);
}

function playClick(ctx: AudioContext, when: number, isDownbeat: boolean) {
  const osc = ctx.createOscillator();
  const gain = ctx.createGain();
  osc.type = "sine";
  osc.frequency.value = isDownbeat ? 1500 : 1000;
  gain.gain.setValueAtTime(0.0001, when);
  gain.gain.exponentialRampToValueAtTime(isDownbeat ? 0.18 : 0.08, when + 0.001);
  gain.gain.exponentialRampToValueAtTime(0.0001, when + 0.05);
  osc.connect(gain).connect(ctx.destination);
  osc.start(when);
  osc.stop(when + 0.06);
}

// ---------------------------------------------------------------------------
// Fallback when peaks aren't available (no ffmpeg, weird file, etc.)
// ---------------------------------------------------------------------------

function FallbackAudio({ trackHash, note }: { trackHash: string; note: string }) {
  return (
    <div className="space-y-2">
      <div className="rounded border border-amber-900/40 bg-amber-950/20 p-3 text-xs text-amber-300">
        Waveform unavailable: {note}. Audio still streams from the backend.
      </div>
      <audio src={api.audioUrl(trackHash)} controls className="w-full" />
    </div>
  );
}
