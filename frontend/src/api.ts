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

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${text}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  health: () => req<Health>("/health"),
  listPlugins: () => req<Plugin[]>("/plugins"),
  callPlugin: (name: string, method: string, params: unknown = {}) =>
    req<{ result: unknown }>(`/plugins/${name}/call`, {
      method: "POST",
      body: JSON.stringify({ method, params }),
    }),
  ingestTrack: (path: string) =>
    req<Track>("/tracks/ingest", {
      method: "POST",
      body: JSON.stringify({ path }),
    }),
  listTracks: () => req<Track[]>("/tracks"),
  getTrack: (hash: string) => req<Track>(`/tracks/${hash}`),
  /** Streaming URL the <audio> element can point at directly (range-served). */
  audioUrl: (hash: string) => `/api/tracks/${hash}/audio`,
  /** Precomputed peaks + duration so WaveSurfer doesn't decode the whole audio. */
  getPeaks: (hash: string, samples = 2048) =>
    req<Peaks>(`/tracks/${hash}/peaks?samples=${samples}`),
  analyzeTrack: (
    hash: string,
    analyzer: string,
    body: { force?: boolean; timeout?: number | null } = {},
  ) =>
    req<AnalysisRun>(`/tracks/${hash}/analyze/${analyzer}`, {
      method: "POST",
      body: JSON.stringify(body),
    }),
  listAnalyses: (hash: string) => req<AnalysisRun[]>(`/tracks/${hash}/analyses`),
  listLabels: (runId: number) =>
    req<AnalysisLabel[]>(`/analyses/${runId}/labels`),
  addLabel: (runId: number, kind: AnalysisLabelKind, notes?: string) =>
    req<AnalysisLabel>(`/analyses/${runId}/labels`, {
      method: "POST",
      body: JSON.stringify({ kind, notes }),
    }),
  deleteLabel: (runId: number, labelId: number) =>
    fetch(`/api/analyses/${runId}/labels/${labelId}`, { method: "DELETE" }).then(
      (r) => {
        if (!r.ok && r.status !== 204) throw new Error(`${r.status} ${r.statusText}`);
      },
    ),
  enqueueJob: (kind: string, payload: unknown = {}) =>
    req<{ id: number }>("/jobs", {
      method: "POST",
      body: JSON.stringify({ kind, payload }),
    }),
  listJobs: () => req<Job[]>("/jobs"),
};
