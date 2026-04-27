/**
 * Per-track view: metadata, waveform, run an analyzer, see the runs that
 * have completed against this track.
 *
 * Beat grid + section overlays + click-track verification arrive in
 * follow-up pushes; for now this is the "make existing data visible"
 * milestone.
 */
import { useCallback, useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  type AnalysisRun,
  type Plugin,
  type Track,
} from "../api";
import { Waveform } from "../components/Waveform";
import { Section, StatusPill } from "../components/ui";
import { fmtBytes } from "../lib/format";

/** Shape of analyzer output we know about today (allin1, allin1_remote). */
type BeatGridOutput = {
  tempo?: { bpm?: number; confidence?: number | null };
  beats?: Array<{ time_sec: number; is_downbeat: boolean }>;
  sections?: Array<{ start_sec: number; end_sec: number; label: string }>;
  duration_sec?: number;
  confidence?: number | null;
};

export function TrackDetailPage() {
  const { hash } = useParams<{ hash: string }>();
  const [track, setTrack] = useState<Track | null>(null);
  const [trackError, setTrackError] = useState<string | null>(null);
  const [trackLoaded, setTrackLoaded] = useState(false);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [analyses, setAnalyses] = useState<AnalysisRun[]>([]);
  const [selectedAnalyzer, setSelectedAnalyzer] = useState<string>("");
  const [running, setRunning] = useState<boolean>(false);
  const [runError, setRunError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    if (!hash) return;
    // Each fetch handled separately so a 404 on one doesn't strand the page.
    try {
      const t = await api.getTrack(hash);
      setTrack(t);
      setTrackError(null);
    } catch (e) {
      setTrackError((e as Error).message);
    } finally {
      setTrackLoaded(true);
    }
    try {
      const ps = await api.listPlugins();
      setPlugins(ps);
    } catch (e) {
      console.error("listPlugins failed", e);
    }
    try {
      const as = await api.listAnalyses(hash);
      setAnalyses(as);
    } catch (e) {
      // 404 just means no rows yet for an unknown track; leave empty.
      console.error("listAnalyses failed", e);
      setAnalyses([]);
    }
  }, [hash]);

  useEffect(() => {
    void refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, [refresh]);

  // Default the analyzer dropdown to the first plugin once they're loaded.
  useEffect(() => {
    if (!selectedAnalyzer && plugins.length > 0) {
      setSelectedAnalyzer(plugins[0].name);
    }
  }, [plugins, selectedAnalyzer]);

  async function runAnalyzer(force: boolean) {
    if (!hash || !selectedAnalyzer) return;
    setRunning(true);
    setRunError(null);
    try {
      await api.analyzeTrack(hash, selectedAnalyzer, { force });
      await refresh();
    } catch (e) {
      setRunError((e as Error).message);
    } finally {
      setRunning(false);
    }
  }

  if (!hash) return <p className="text-sm text-zinc-500">no track hash</p>;
  if (!trackLoaded) {
    return (
      <div className="space-y-4">
        <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← library
        </Link>
        <p className="text-sm text-zinc-500">loading track…</p>
      </div>
    );
  }
  if (!track) {
    return (
      <div className="space-y-4">
        <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← library
        </Link>
        <p className="text-sm text-zinc-100">track not found</p>
        <p className="break-all font-mono text-xs text-zinc-500">{hash}</p>
        {trackError && (
          <p className="text-xs text-zinc-600">{trackError}</p>
        )}
      </div>
    );
  }

  const filename = track.source_path.split("/").pop() ?? track.content_hash;

  return (
    <div className="space-y-8">
      <div>
        <Link to="/" className="text-xs text-zinc-500 hover:text-zinc-300">
          ← library
        </Link>
        <h2 className="mt-1 break-all text-xl text-zinc-100">{filename}</h2>
        <div className="mt-1 break-all font-mono text-xs text-zinc-500">
          {track.content_hash}
        </div>
      </div>

      <Section title="Track">
        <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm font-mono">
          <dt className="text-zinc-500">format</dt>
          <dd>{track.format ?? "—"}</dd>
          <dt className="text-zinc-500">size</dt>
          <dd>{fmtBytes(track.file_size)}</dd>
          {track.duration_sec !== null && (
            <>
              <dt className="text-zinc-500">duration</dt>
              <dd>{track.duration_sec.toFixed(1)}s</dd>
            </>
          )}
          <dt className="text-zinc-500">path</dt>
          <dd className="break-all">{track.source_path}</dd>
        </dl>
      </Section>

      <Section title="Waveform">
        <Waveform trackHash={hash} />
      </Section>

      <Section title="Run analyzer">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={selectedAnalyzer}
            onChange={(e) => setSelectedAnalyzer(e.target.value)}
            className="rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm font-mono"
          >
            {plugins.map((p) => (
              <option key={p.name} value={p.name}>
                {p.name}
                {p.cloud_audio ? " (cloud)" : ""}
              </option>
            ))}
          </select>
          <button
            onClick={() => runAnalyzer(false)}
            disabled={running || !selectedAnalyzer}
            className="rounded bg-blue-600 px-4 py-2 text-sm hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {running ? "running…" : "run"}
          </button>
          <button
            onClick={() => runAnalyzer(true)}
            disabled={running || !selectedAnalyzer}
            className="rounded bg-zinc-700 px-3 py-2 text-xs hover:bg-zinc-600 disabled:cursor-not-allowed disabled:opacity-50"
            title="Re-run even if a completed result is already cached"
          >
            force re-run
          </button>
          {runError && <span className="text-xs text-red-400">{runError}</span>}
        </div>
        {selectedAnalyzer &&
          plugins.find((p) => p.name === selectedAnalyzer)?.cloud_audio && (
            <p className="mt-2 text-xs text-amber-300">
              this analyzer uploads audio off-machine — backend must be started with
              <code className="mx-1 rounded bg-amber-950/40 px-1 py-0.5">
                AIDJ_ALLOW_CLOUD_AUDIO=1
              </code>
            </p>
          )}
      </Section>

      <Section title="Analyses">
        {analyses.length === 0 ? (
          <p className="text-sm text-zinc-500">no runs yet</p>
        ) : (
          <ul className="space-y-2">
            {analyses.map((r) => (
              <AnalysisCard key={r.id} run={r} />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function AnalysisCard({ run }: { run: AnalysisRun }) {
  const out = run.output as BeatGridOutput | null;
  return (
    <li className="rounded-md border border-zinc-800 bg-zinc-900/50 p-3">
      <div className="flex items-center gap-2">
        <span className="font-mono text-sm text-zinc-100">{run.analyzer_name}</span>
        <span className="text-xs text-zinc-500">v{run.analyzer_version}</span>
        <span className="ml-auto">
          <StatusPill status={run.status} />
        </span>
      </div>
      {run.started_at && (
        <div className="mt-1 text-[10px] text-zinc-600">
          {run.started_at}
          {run.finished_at && run.finished_at !== run.started_at && (
            <> → {run.finished_at}</>
          )}
        </div>
      )}
      {run.error && (
        <pre className="mt-2 overflow-x-auto rounded bg-black/50 p-2 text-xs text-red-300">
          {run.error}
        </pre>
      )}
      {out && (
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          {out.tempo?.bpm !== undefined && (
            <>
              <dt className="text-zinc-500">tempo</dt>
              <dd>
                {out.tempo.bpm.toFixed(1)} BPM
                {out.tempo.confidence != null && (
                  <span className="ml-2 text-zinc-600">
                    conf {out.tempo.confidence.toFixed(2)}
                  </span>
                )}
              </dd>
            </>
          )}
          {out.beats && (
            <>
              <dt className="text-zinc-500">beats</dt>
              <dd>
                {out.beats.length}{" "}
                <span className="text-zinc-600">
                  ({out.beats.filter((b) => b.is_downbeat).length} downbeats)
                </span>
              </dd>
            </>
          )}
          {out.sections && out.sections.length > 0 && (
            <>
              <dt className="text-zinc-500">sections</dt>
              <dd className="break-all">
                {out.sections.map((s) => s.label).join(" → ")}
              </dd>
            </>
          )}
          {out.duration_sec !== undefined && (
            <>
              <dt className="text-zinc-500">duration</dt>
              <dd>{out.duration_sec.toFixed(1)}s</dd>
            </>
          )}
        </dl>
      )}
    </li>
  );
}
