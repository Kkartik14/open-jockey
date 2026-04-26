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
  enqueueJob: (kind: string, payload: unknown = {}) =>
    req<{ id: number }>("/jobs", {
      method: "POST",
      body: JSON.stringify({ kind, payload }),
    }),
  listJobs: () => req<Job[]>("/jobs"),
};
