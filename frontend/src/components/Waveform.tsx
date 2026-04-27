/**
 * Waveform display backed by precomputed peaks.
 *
 * The component fetches ``/api/tracks/{hash}/peaks`` for the visual, and
 * hands WaveSurfer an HTMLAudioElement pointing at ``/api/tracks/{hash}/audio``
 * for playback. Crucially we do NOT pass ``url`` to WaveSurfer — that would
 * make it fetch + decode the entire blob to draw the waveform itself, which
 * negates the backend's Range support and falls over on long lossless tracks.
 *
 * If the peaks endpoint returns 503 (no ffmpeg) or any other error, we fall
 * back to a plain ``<audio>`` element with controls so playback still works.
 */
import { useEffect, useRef, useState } from "react";
import WaveSurfer from "wavesurfer.js";
import { api, type Peaks } from "../api";

interface WaveformProps {
  trackHash: string;
}

export function Waveform({ trackHash }: WaveformProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const wsRef = useRef<WaveSurfer | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);

  const [peaks, setPeaks] = useState<Peaks | null>(null);
  const [peaksError, setPeaksError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [playing, setPlaying] = useState(false);

  // Fetch peaks before mounting the player.
  useEffect(() => {
    let active = true;
    setPeaks(null);
    setPeaksError(null);
    setReady(false);
    api
      .getPeaks(trackHash)
      .then((p) => {
        if (active) setPeaks(p);
      })
      .catch((e: Error) => {
        if (active) setPeaksError(e.message);
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

    ws.on("ready", () => setReady(true));
    ws.on("play", () => setPlaying(true));
    ws.on("pause", () => setPlaying(false));
    ws.on("finish", () => setPlaying(false));

    // WaveSurfer fires 'ready' from the decode path; with peaks+media we get
    // it on the first 'canplay' from the <audio>. Guard with a fallback.
    if (audio.readyState >= 2) setReady(true);

    return () => {
      ws.destroy();
      wsRef.current = null;
      audio.pause();
      audio.src = "";
      audioRef.current = null;
    };
  }, [peaks, trackHash]);

  function toggle() {
    void wsRef.current?.playPause();
  }

  if (peaksError) {
    return <FallbackAudio trackHash={trackHash} note={peaksError} />;
  }

  return (
    <div className="space-y-2">
      <div ref={containerRef} className="rounded bg-zinc-900/50 px-2 py-3" />
      <div className="flex items-center gap-3 text-xs text-zinc-400">
        <button
          type="button"
          onClick={toggle}
          disabled={!peaks || !ready}
          className="rounded bg-zinc-700 px-3 py-1 text-zinc-100 hover:bg-zinc-600 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {playing ? "pause" : "play"}
        </button>
        {!peaks && <span>computing peaks…</span>}
        {peaks && !ready && <span>loading audio…</span>}
        {peaks && (
          <span>
            {peaks.duration_sec.toFixed(1)}s · {peaks.samples} peak buckets
          </span>
        )}
      </div>
    </div>
  );
}

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
