// API client — talks to the backend through Vite's /api proxy.

export type Health = {
  status: string;
  version: string;
  project_root: string;
  store_root: string;
  schema_version: number | null;
};

export type Plugin = {
  name: string;
  version: string;
  description: string;
  python: string;
  hardware: { cpu_cores: number; ram_mb: number; gpu: string };
  concurrency_safe: boolean;
  default_timeout_sec: number;
  cloud_audio: boolean;
};

export type Track = {
  content_hash: string;
  source_path: string;
  duration_sec: number | null;
  sample_rate: number | null;
  channels: number | null;
  format: string | null;
  bitrate: number | null;
  file_size: number | null;
  genre: string | null;
};

export type Job = {
  id: number;
  kind: string;
  status: string;
  retries: number;
  max_retries: number;
  error: string | null;
  result: unknown;
};

export type Peaks = {
  duration_sec: number;
  samples: number;
  peaks: number[];
};

export type AnalysisRun = {
  id: number;
  track_hash: string;
  analyzer_name: string;
  analyzer_version: string;
  status: "pending" | "running" | "completed" | "failed";
  output: Record<string, unknown> | null;
  confidence: number | null;
  error: string | null;
  started_at: string | null;
  finished_at: string | null;
  /**
   * Verification labels — populated by ``/api/tracks/{hash}/analyses`` (which
   * returns the ``AnalysisRunDetail`` shape on the backend). Absent on the
   * single-run endpoint and immediately after a fresh ``analyzeTrack`` call.
   */
  labels?: AnalysisLabel[];
};

export type AnalysisLabelKind =
  | "correct"
  | "half_time"
  | "double_time"
  | "wrong_downbeat_phase"
  | "early_by_ms"
  | "late_by_ms"
  | "wrong_section_labels"
  | "unusable";

export type AnalysisLabel = {
  id: number;
  analysis_run_id: number;
  kind: AnalysisLabelKind;
  notes: string | null;
  created_at: string | null;
};

/**
 * Cross-track bake-off summary returned by ``GET /api/labels/rollup``.
 * Drives the Library page's rollup table.
 */
export type LabelRollup = {
  by_analyzer: Record<string, Partial<Record<AnalysisLabelKind, number>>>;
  by_analyzer_and_genre: Record<
    string,
    Record<string, Partial<Record<AnalysisLabelKind, number>>>
  >;
  total_labels: number;
  total_labeled_runs: number;
};

// ---------------------------------------------------------------------------
// TrackProfile (Phase 2) — hand-mirrored from backend/aidj/store/models.py.
// If these drift, run the listed tests on both sides; OpenAPI codegen is the
// right long-term fix.
// ---------------------------------------------------------------------------

export type FieldProvenance = {
  source: string;
  analysis_run_id: number | null;
};

export type TempoBlock = {
  bpm: number;
  confidence: number | null;
  provenance: FieldProvenance;
};

export type BeatMarkProfile = {
  time_sec: number;
  is_downbeat: boolean;
  confidence: number | null;
};

export type BeatGridBlock = {
  beats: BeatMarkProfile[];
  downbeat_count: number;
  duration_sec: number;
  provenance: FieldProvenance;
};

export type KeyBlock = {
  key: string;
  scale: string;
  camelot: string | null;
  confidence: number | null;
  provenance: FieldProvenance;
};

export type SectionItemProfile = {
  start_sec: number;
  end_sec: number;
  label: string;
  confidence: number | null;
};

export type SectionsBlock = {
  items: SectionItemProfile[];
  provenance: FieldProvenance;
};

export type EnergyBlock = {
  sample_rate_hz: number;
  values: number[];
  integrated_lufs: number | null;
  section_energy: Record<string, number>;
  drop_times_sec: number[];
  build_times_sec: number[];
  provenance: FieldProvenance;
};

export type VocalWindowProfile = {
  start_sec: number;
  end_sec: number;
  is_vocal: boolean;
  confidence: number | null;
};

export type VocalsBlock = {
  windows: VocalWindowProfile[];
  stem_cache_key: string | null;
  provenance: FieldProvenance;
};

export type CompletenessFields = {
  has_beat_grid: boolean;
  has_key: boolean;
  has_sections: boolean;
  has_energy: boolean;
  has_vocals: boolean;
};

export type Readiness = "ready" | "partial" | "blocked";

export type TrackProfile = {
  profile_version: number;
  track_hash: string;
  built_at: string;
  readiness: Readiness;
  completeness_score: number;
  fields: CompletenessFields;
  tempo: TempoBlock | null;
  beat_grid: BeatGridBlock | null;
  key: KeyBlock | null;
  sections: SectionsBlock | null;
  energy: EnergyBlock | null;
  vocals: VocalsBlock | null;
};

export type ProfileCoverage = {
  ready: number;
  partial: number;
  blocked: number;
  missing: number;
};

/**
 * Per-request options. ``signal`` cancels the fetch when an AbortController is
 * aborted (cleanup on unmount). ``timeoutMs`` aborts on a deadline — useful
 * for /peaks, which decodes a whole audio file and can take seconds.
 */
export type RequestOptions = {
  signal?: AbortSignal;
  timeoutMs?: number;
};

function mergeSignals(opts: RequestOptions | undefined): {
  signal: AbortSignal | undefined;
  cleanup: () => void;
} {
  if (!opts || (!opts.signal && !opts.timeoutMs)) {
    return { signal: undefined, cleanup: () => {} };
  }
  if (opts.timeoutMs === undefined) {
    return { signal: opts.signal, cleanup: () => {} };
  }
  // Compose caller signal + timeout into one controller.
  const controller = new AbortController();
  const timer = setTimeout(
    () => controller.abort(new Error(`request timed out after ${opts.timeoutMs}ms`)),
    opts.timeoutMs,
  );
  if (opts.signal) {
    if (opts.signal.aborted) {
      controller.abort(opts.signal.reason);
    } else {
      opts.signal.addEventListener(
        "abort",
        () => controller.abort(opts.signal!.reason),
        { once: true },
      );
    }
  }
  return {
    signal: controller.signal,
    cleanup: () => clearTimeout(timer),
  };
}

async function req<T>(
  path: string,
  init?: RequestInit,
  opts?: RequestOptions,
): Promise<T> {
  const { signal, cleanup } = mergeSignals(opts);
  try {
    const res = await fetch(`/api${path}`, {
      headers: { "Content-Type": "application/json" },
      signal,
      ...init,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status} ${res.statusText}: ${text}`);
    }
    return (await res.json()) as T;
  } finally {
    cleanup();
  }
}

export const api = {
  health: (opts?: RequestOptions) => req<Health>("/health", undefined, opts),
  listPlugins: (opts?: RequestOptions) =>
    req<Plugin[]>("/plugins", undefined, opts),
  callPlugin: (name: string, method: string, params: unknown = {}, opts?: RequestOptions) =>
    req<{ result: unknown }>(
      `/plugins/${name}/call`,
      { method: "POST", body: JSON.stringify({ method, params }) },
      opts,
    ),
  ingestTrack: (path: string, opts?: RequestOptions) =>
    req<Track>(
      "/tracks/ingest",
      { method: "POST", body: JSON.stringify({ path }) },
      opts,
    ),
  listTracks: (opts?: RequestOptions) =>
    req<Track[]>("/tracks", undefined, opts),
  getTrack: (hash: string, opts?: RequestOptions) =>
    req<Track>(`/tracks/${hash}`, undefined, opts),
  /** Patch a track's user-editable metadata. Today only ``genre``. */
  setTrackGenre: (hash: string, genre: string | null, opts?: RequestOptions) =>
    req<Track>(
      `/tracks/${hash}`,
      { method: "PATCH", body: JSON.stringify({ genre }) },
      opts,
    ),
  /** Streaming URL the <audio> element can point at directly (range-served). */
  audioUrl: (hash: string) => `/api/tracks/${hash}/audio`,
  /** Precomputed peaks + duration so WaveSurfer doesn't decode the whole audio.
   *  Decoding can take a few seconds on long tracks — pass a signal/timeoutMs
   *  to abort if the user navigates away or the request hangs. */
  getPeaks: (hash: string, samples = 2048, opts?: RequestOptions) =>
    req<Peaks>(`/tracks/${hash}/peaks?samples=${samples}`, undefined, opts),
  analyzeTrack: (
    hash: string,
    analyzer: string,
    body: { force?: boolean; timeout?: number | null } = {},
    opts?: RequestOptions,
  ) =>
    req<AnalysisRun>(
      `/tracks/${hash}/analyze/${analyzer}`,
      { method: "POST", body: JSON.stringify(body) },
      opts,
    ),
  listAnalyses: (hash: string, opts?: RequestOptions) =>
    req<AnalysisRun[]>(`/tracks/${hash}/analyses`, undefined, opts),
  listLabels: (runId: number, opts?: RequestOptions) =>
    req<AnalysisLabel[]>(`/analyses/${runId}/labels`, undefined, opts),
  addLabel: (runId: number, kind: AnalysisLabelKind, notes?: string, opts?: RequestOptions) =>
    req<AnalysisLabel>(
      `/analyses/${runId}/labels`,
      { method: "POST", body: JSON.stringify({ kind, notes }) },
      opts,
    ),
  deleteLabel: (runId: number, labelId: number, opts?: RequestOptions) => {
    const { signal, cleanup } = mergeSignals(opts);
    return fetch(`/api/analyses/${runId}/labels/${labelId}`, {
      method: "DELETE",
      signal,
    })
      .then((r) => {
        if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
      })
      .finally(cleanup);
  },
  enqueueJob: (kind: string, payload: unknown = {}, opts?: RequestOptions) =>
    req<{ id: number }>(
      "/jobs",
      { method: "POST", body: JSON.stringify({ kind, payload }) },
      opts,
    ),
  listJobs: (opts?: RequestOptions) => req<Job[]>("/jobs", undefined, opts),
  /** Cross-track bake-off rollup (per-analyzer and per-analyzer-per-genre). */
  getLabelRollup: (opts?: RequestOptions) =>
    req<LabelRollup>("/labels/rollup", undefined, opts),

  // -------------------------------------------------------------------------
  // Track profiles (Phase 2)
  // -------------------------------------------------------------------------

  /** Read-only fetch — 404 distinguishes "track missing" from "profile missing". */
  getProfile: (hash: string, opts?: RequestOptions) =>
    req<TrackProfile>(`/tracks/${hash}/profile`, undefined, opts),
  /** Synchronous rebuild — always force=true on the backend; no body. */
  buildProfile: (hash: string, opts?: RequestOptions) =>
    req<TrackProfile>(
      `/tracks/${hash}/profile/build`,
      { method: "POST" },
      opts,
    ),
  /** Library-wide coverage bucket counts (ready+partial+blocked+missing = total). */
  getProfileCoverage: (opts?: RequestOptions) =>
    req<ProfileCoverage>("/profiles/coverage", undefined, opts),
};
