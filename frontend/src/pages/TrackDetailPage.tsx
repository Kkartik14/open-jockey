/**
 * Per-track view: metadata, waveform with selectable beat-grid + section
 * overlay, run an analyzer, see (and label) the runs that have completed
 * against this track.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef } from "react";
import { Link, useParams } from "react-router-dom";
import {
  api,
  type AnalysisLabel,
  type AnalysisLabelKind,
  type AnalysisRun,
  type Plugin,
  type Track,
} from "../api";
import { Waveform, type BeatMark, type SectionMark } from "../components/Waveform";
import { Section, StatusPill } from "../components/ui";
import { fmtBytes } from "../lib/format";

/** Shape of analyzer output we know about today (allin1, allin1_remote). */
type BeatGridOutput = {
  tempo?: { bpm?: number; confidence?: number | null };
  beats: BeatMark[];
  sections: SectionMark[];
  duration_sec?: number;
  confidence?: number | null;
};

/** Shape of a future essentia-style key-detection output. */
type KeyOutput = {
  key?: string;
  scale?: string;
  camelot?: string;
  confidence?: number | null;
};

const LABEL_KINDS: ReadonlyArray<{ kind: AnalysisLabelKind; tag: string; tone: string }> = [
  { kind: "correct", tag: "✓", tone: "bg-emerald-900/40 text-emerald-300" },
  { kind: "half_time", tag: "½×", tone: "bg-amber-900/40 text-amber-300" },
  { kind: "double_time", tag: "2×", tone: "bg-amber-900/40 text-amber-300" },
  { kind: "wrong_downbeat_phase", tag: "phase", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "early_by_ms", tag: "early", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "late_by_ms", tag: "late", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "wrong_section_labels", tag: "sections", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "unusable", tag: "unusable", tone: "bg-red-900/40 text-red-300" },
];

type TrackDetailState = {
  track: Track | null;
  trackError: string | null;
  trackLoaded: boolean;
  plugins: Plugin[];
  analyses: AnalysisRun[];
  selectedAnalyzer: string;
  running: boolean;
  runError: string | null;
  overlayRunId: number | null;
};

const INITIAL_TRACK_DETAIL_STATE: TrackDetailState = {
  track: null,
  trackError: null,
  trackLoaded: false,
  plugins: [],
  analyses: [],
  selectedAnalyzer: "",
  running: false,
  runError: null,
  overlayRunId: null,
};

function trackDetailReducer(
  state: TrackDetailState,
  patch: Partial<TrackDetailState>,
): TrackDetailState {
  return { ...state, ...patch };
}

export function TrackDetailPage() {
  const { hash } = useParams<{ hash: string }>();
  if (!hash) return <p className="text-sm text-zinc-500">no track hash</p>;
  return <TrackDetailContent key={hash} hash={hash} />;
}

function TrackDetailContent({ hash }: { hash: string }) {
  const [state, dispatchDetail] = useReducer(
    trackDetailReducer,
    INITIAL_TRACK_DETAIL_STATE,
  );
  const {
    track,
    trackError,
    trackLoaded,
    plugins,
    analyses,
    selectedAnalyzer,
    running,
    runError,
    overlayRunId,
  } = state;
  const mountedRef = useRef(true);
  const refreshSeqRef = useRef(0);

  useEffect(
    () => () => {
      mountedRef.current = false;
    },
    [],
  );

  const refresh = useCallback(async () => {
    const seq = ++refreshSeqRef.current;
    const isFresh = () => mountedRef.current && seq === refreshSeqRef.current;
    if (!mountedRef.current) return;

    const [trackResult, pluginsResult, analysesResult] = await Promise.allSettled([
      api.getTrack(hash),
      api.listPlugins(),
      api.listAnalyses(hash),
    ]);

    // Only the newest refresh for this mounted route instance may write state.
    // Route changes remount this component via key={hash}; this guard handles
    // slower older requests from the same instance as well.
    if (isFresh()) {
      const patch: Partial<TrackDetailState> = { trackLoaded: true };
      if (trackResult.status === "fulfilled") {
        patch.track = trackResult.value;
        patch.trackError = null;
      } else {
        patch.track = null;
        patch.trackError = errorMessage(trackResult.reason);
      }

      if (pluginsResult.status === "fulfilled") {
        patch.plugins = pluginsResult.value;
      } else {
        console.error("listPlugins failed", pluginsResult.reason);
      }

      if (analysesResult.status === "fulfilled") {
        patch.analyses = analysesResult.value;
      } else {
        console.error("listAnalyses failed", analysesResult.reason);
      }
      dispatchDetail(patch);
    }
  }, [hash]);

  useEffect(() => {
    void refresh();
    const t = setInterval(() => void refresh(), 5000);
    return () => clearInterval(t);
  }, [refresh]);

  // Don't auto-select an analyzer. Discovery is alphabetical so the first
  // plugin is whichever is alphabetically first (today: ``allin1``, which is
  // currently broken locally). Empty default + a "select…" placeholder forces
  // a deliberate choice and disables the run button until then.

  // Default the overlay to the most recent COMPLETED analysis whose output
  // looks like a BeatGridAnalysis. The backend's list_for_track now sorts by
  // started_at DESC across all analyzers, so analyses[0] is the actual most
  // recent (not just alphabetically first).
  useEffect(() => {
    if (overlayRunId !== null) return;
    const candidate = analyses.find(
      (r) => r.status === "completed" && hasBeats(r.output),
    );
    if (candidate) dispatchDetail({ overlayRunId: candidate.id });
  }, [analyses, overlayRunId]);

  const overlayRun = useMemo(
    () => analyses.find((r) => r.id === overlayRunId) ?? null,
    [analyses, overlayRunId],
  );
  const overlayOutput = getBeatGridOutput(overlayRun?.output);
  const overlayBeats = overlayOutput?.beats ?? undefined;
  const overlaySections = overlayOutput?.sections ?? undefined;

  // Only completed runs that produced a BeatGridAnalysis can drive the
  // overlay selector.
  const beatGridRuns = useMemo(
    () => analyses.filter((r) => r.status === "completed" && hasBeats(r.output)),
    [analyses],
  );

  async function runAnalyzer(force: boolean) {
    if (!selectedAnalyzer) return;
    const analyzer = selectedAnalyzer;
    dispatchDetail({ running: true, runError: null });
    try {
      await api.analyzeTrack(hash, analyzer, { force });
      await refresh();
    } catch (e) {
      if (mountedRef.current) dispatchDetail({ runError: errorMessage(e) });
    } finally {
      if (mountedRef.current) dispatchDetail({ running: false });
    }
  }

  async function addLabel(runId: number, kind: AnalysisLabelKind) {
    try {
      await api.addLabel(runId, kind);
      await refresh();
    } catch (e) {
      if (mountedRef.current) console.error("addLabel failed", e);
    }
  }

  async function removeLabel(runId: number, labelId: number) {
    try {
      await api.deleteLabel(runId, labelId);
      await refresh();
    } catch (e) {
      if (mountedRef.current) console.error("deleteLabel failed", e);
    }
  }

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
        {trackError && <p className="text-xs text-zinc-600">{trackError}</p>}
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
        {beatGridRuns.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-2 text-xs text-zinc-400">
            <span>overlay:</span>
            <button
              type="button"
              onClick={() => dispatchDetail({ overlayRunId: null })}
              className={`rounded px-2 py-1 font-mono ${
                overlayRunId === null
                  ? "bg-zinc-700 text-zinc-100"
                  : "bg-zinc-900 text-zinc-500 hover:bg-zinc-800"
              }`}
            >
              none
            </button>
            {beatGridRuns.map((r) => (
              <button
                key={r.id}
                type="button"
                onClick={() => dispatchDetail({ overlayRunId: r.id })}
                className={`rounded px-2 py-1 font-mono ${
                  overlayRunId === r.id
                    ? "bg-purple-900/60 text-purple-100"
                    : "bg-zinc-900 text-zinc-400 hover:bg-zinc-800"
                }`}
              >
                {r.analyzer_name}
              </button>
            ))}
          </div>
        )}
        <Waveform
          trackHash={hash}
          beats={overlayBeats}
          sections={overlaySections}
        />
      </Section>

      <Section title="Run analyzer">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={selectedAnalyzer}
            onChange={(e) => dispatchDetail({ selectedAnalyzer: e.target.value })}
            className="rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm font-mono"
          >
            <option value="">select analyzer…</option>
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
              <AnalysisCard
                key={r.id}
                run={r}
                labels={r.labels ?? []}
                onAddLabel={(kind) => addLabel(r.id, kind)}
                onRemoveLabel={(labelId) => removeLabel(r.id, labelId)}
              />
            ))}
          </ul>
        )}
      </Section>
    </div>
  );
}

function hasBeats(output: unknown): output is BeatGridOutput {
  return (getBeatGridOutput(output)?.beats.length ?? 0) > 0;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function finiteNumber(value: unknown): number | undefined {
  return typeof value === "number" && Number.isFinite(value) ? value : undefined;
}

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function isBeatMark(value: unknown): value is BeatMark {
  if (!isRecord(value)) return false;
  const time = finiteNumber(value.time_sec);
  return time !== undefined && time >= 0 && typeof value.is_downbeat === "boolean";
}

function isSectionMark(value: unknown): value is SectionMark {
  if (!isRecord(value)) return false;
  const start = finiteNumber(value.start_sec);
  const end = finiteNumber(value.end_sec);
  return (
    start !== undefined &&
    end !== undefined &&
    start >= 0 &&
    end > start &&
    typeof value.label === "string" &&
    value.label.trim().length > 0
  );
}

function getBeatGridOutput(output: unknown): BeatGridOutput | null {
  if (!isRecord(output) || !Array.isArray(output.beats)) return null;

  const beats = output.beats
    .filter(isBeatMark)
    .slice()
    .sort((a, b) => a.time_sec - b.time_sec);
  const rawSections = Array.isArray(output.sections) ? output.sections : [];
  const sections: SectionMark[] = [];
  for (const rawSection of rawSections) {
    if (!isSectionMark(rawSection)) continue;
    sections.push({ ...rawSection, label: rawSection.label.trim() });
  }
  sections.sort((a, b) => a.start_sec - b.start_sec);

  const parsed: BeatGridOutput = { beats, sections };
  if (isRecord(output.tempo)) {
    const bpm = finiteNumber(output.tempo.bpm);
    const confidence = finiteNumber(output.tempo.confidence);
    if (bpm !== undefined || confidence !== undefined) {
      parsed.tempo = { bpm, confidence };
    }
  }

  const duration = finiteNumber(output.duration_sec);
  if (duration !== undefined && duration >= 0) parsed.duration_sec = duration;

  const confidence = finiteNumber(output.confidence);
  if (confidence !== undefined) parsed.confidence = confidence;

  return parsed;
}

function getKeyOutput(output: unknown): KeyOutput | null {
  if (!isRecord(output) || typeof output.key !== "string") return null;
  const key = output.key.trim();
  if (!key) return null;

  const parsed: KeyOutput = { key };
  if (typeof output.scale === "string" && output.scale.trim()) {
    parsed.scale = output.scale.trim();
  }
  if (typeof output.camelot === "string" && output.camelot.trim()) {
    parsed.camelot = output.camelot.trim();
  }
  const confidence = finiteNumber(output.confidence);
  if (confidence !== undefined) parsed.confidence = confidence;
  return parsed;
}

interface AnalysisCardProps {
  run: AnalysisRun;
  labels: AnalysisLabel[];
  onAddLabel: (kind: AnalysisLabelKind) => void;
  onRemoveLabel: (labelId: number) => void;
}

function AnalysisCard({ run, labels, onAddLabel, onRemoveLabel }: AnalysisCardProps) {
  const beatOutput = getBeatGridOutput(run.output);
  const keyOutput = beatOutput ? null : getKeyOutput(run.output);
  const isBeatGrid = beatOutput !== null;
  const isKey = keyOutput !== null;

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
      {isBeatGrid && beatOutput && (
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          {beatOutput.tempo?.bpm !== undefined && (
            <>
              <dt className="text-zinc-500">tempo</dt>
              <dd>
                {beatOutput.tempo.bpm.toFixed(1)} BPM
                {beatOutput.tempo.confidence != null && (
                  <span className="ml-2 text-zinc-600">
                    conf {beatOutput.tempo.confidence.toFixed(2)}
                  </span>
                )}
              </dd>
            </>
          )}
          {beatOutput.beats && (
            <>
              <dt className="text-zinc-500">beats</dt>
              <dd>
                {beatOutput.beats.length}{" "}
                <span className="text-zinc-600">
                  ({beatOutput.beats.filter((b) => b.is_downbeat).length} downbeats)
                </span>
              </dd>
            </>
          )}
          {beatOutput.sections && beatOutput.sections.length > 0 && (
            <>
              <dt className="text-zinc-500">sections</dt>
              <dd className="break-all">
                {beatOutput.sections.map((s) => s.label).join(" → ")}
              </dd>
            </>
          )}
          {beatOutput.duration_sec !== undefined && (
            <>
              <dt className="text-zinc-500">duration</dt>
              <dd>{beatOutput.duration_sec.toFixed(1)}s</dd>
            </>
          )}
        </dl>
      )}
      {isKey && keyOutput && (
        <dl className="mt-2 grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-xs font-mono">
          <dt className="text-zinc-500">key</dt>
          <dd>
            {keyOutput.key}
            {keyOutput.scale ? ` ${keyOutput.scale}` : ""}
            {keyOutput.camelot && (
              <span className="ml-2 text-purple-300">{keyOutput.camelot}</span>
            )}
          </dd>
          {keyOutput.confidence != null && (
            <>
              <dt className="text-zinc-500">confidence</dt>
              <dd>{keyOutput.confidence.toFixed(2)}</dd>
            </>
          )}
        </dl>
      )}
      {(run.status === "completed" || run.status === "failed") && (
        <LabelRow
          labels={labels}
          onAdd={onAddLabel}
          onRemove={onRemoveLabel}
        />
      )}
    </li>
  );
}

function LabelRow({
  labels,
  onAdd,
  onRemove,
}: {
  labels: AnalysisLabel[];
  onAdd: (kind: AnalysisLabelKind) => void;
  onRemove: (labelId: number) => void;
}) {
  // Roll up label counts so the user can see "this got marked correct twice"
  // at a glance. Click an existing label-pill to remove the most recent
  // instance of that kind. We tie-break by id (auto-increment) because
  // SQLite's datetime('now') is per-second and two same-second labels would
  // otherwise compare equal — picking the wrong one to remove.
  const counts = labels.reduce<Record<string, number>>((acc, l) => {
    acc[l.kind] = (acc[l.kind] ?? 0) + 1;
    return acc;
  }, {});
  const latestByKind: Record<string, AnalysisLabel> = {};
  for (const l of labels) {
    const prior = latestByKind[l.kind];
    if (!prior) {
      latestByKind[l.kind] = l;
      continue;
    }
    const lTs = l.created_at ?? "";
    const priorTs = prior.created_at ?? "";
    const lWins =
      lTs > priorTs || (lTs === priorTs && l.id > prior.id);
    if (lWins) latestByKind[l.kind] = l;
  }

  return (
    <div className="mt-3 border-t border-zinc-800/60 pt-2">
      <div className="mb-1.5 text-[10px] uppercase tracking-wider text-zinc-500">
        verify
      </div>
      <div className="flex flex-wrap items-center gap-1.5">
        {LABEL_KINDS.map(({ kind, tag, tone }) => {
          const count = counts[kind] ?? 0;
          const latest = latestByKind[kind];
          return (
            <button
              key={kind}
              type="button"
              onClick={() => {
                if (count > 0 && latest) onRemove(latest.id);
                else onAdd(kind);
              }}
              className={`rounded px-2 py-0.5 text-[10px] font-mono transition ${
                count > 0
                  ? tone
                  : "bg-zinc-900 text-zinc-500 hover:bg-zinc-800 hover:text-zinc-300"
              }`}
              title={
                count > 0
                  ? `${kind} (${count}) — click to remove the most recent`
                  : `mark as ${kind}`
              }
            >
              {tag}
              {count > 0 && <span className="ml-1 opacity-70">×{count}</span>}
            </button>
          );
        })}
      </div>
    </div>
  );
}
