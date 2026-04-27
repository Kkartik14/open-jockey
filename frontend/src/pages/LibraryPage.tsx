/**
 * The home page: backend status, plugins list, ingest, tracks table, jobs.
 *
 * Track rows now link to /track/:hash, where the per-track view lives.
 */
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Health, type Job, type Plugin, type Track } from "../api";
import { Section, StatusPill } from "../components/ui";
import { fmtBytes } from "../lib/format";

export function LibraryPage() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthErr, setHealthErr] = useState<string | null>(null);
  const [plugins, setPlugins] = useState<Plugin[]>([]);
  const [tracks, setTracks] = useState<Track[]>([]);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [pluginOutput, setPluginOutput] = useState<string>("");
  const [ingestPath, setIngestPath] = useState<string>("");

  async function refresh() {
    try {
      const [h, ps, ts, js] = await Promise.all([
        api.health(),
        api.listPlugins(),
        api.listTracks(),
        api.listJobs(),
      ]);
      setHealth(h);
      setHealthErr(null);
      setPlugins(ps);
      setTracks(ts);
      setJobs(js);
    } catch (e) {
      setHealthErr((e as Error).message);
    }
  }

  useEffect(() => {
    refresh();
    const t = setInterval(refresh, 5000);
    return () => clearInterval(t);
  }, []);

  async function pingPlugin(name: string) {
    // ``ping`` is reserved by the SDK and supported by every plugin — safe to
    // call on any name without knowing what methods the plugin implements.
    try {
      const out = await api.callPlugin(name, "ping");
      setPluginOutput(`${name}: ${JSON.stringify(out.result)}`);
    } catch (e) {
      setPluginOutput(`${name}: error: ${(e as Error).message}`);
    }
  }

  async function ingest() {
    if (!ingestPath.trim()) return;
    try {
      await api.ingestTrack(ingestPath.trim());
      setIngestPath("");
      await refresh();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  return (
    <div className="space-y-8">
      <Section title="Backend">
        {health ? (
          <dl className="grid grid-cols-[max-content_1fr] gap-x-6 gap-y-1 text-sm font-mono">
            <dt className="text-zinc-500">version</dt>
            <dd>{health.version}</dd>
            <dt className="text-zinc-500">schema</dt>
            <dd>v{health.schema_version}</dd>
            <dt className="text-zinc-500">project root</dt>
            <dd className="break-all">{health.project_root}</dd>
            <dt className="text-zinc-500">store root</dt>
            <dd className="break-all">{health.store_root}</dd>
          </dl>
        ) : (
          <p className="text-sm text-zinc-500">{healthErr ?? "loading…"}</p>
        )}
      </Section>

      <Section title="Plugins">
        {plugins.length === 0 && <p className="text-sm text-zinc-500">none discovered</p>}
        <ul className="space-y-2">
          {plugins.map((p) => (
            <li
              key={p.name}
              className="flex items-center justify-between gap-4 rounded-md border border-zinc-800 bg-zinc-900/50 px-3 py-2"
            >
              <div>
                <div className="flex items-center gap-2 font-mono text-sm">
                  {p.name}
                  <span className="text-zinc-500"> @ {p.version}</span>
                  {p.cloud_audio && (
                    <span
                      className="rounded bg-amber-900/40 px-1.5 py-0.5 text-[10px] text-amber-300"
                      title="Uploads audio off-machine. Set AIDJ_ALLOW_CLOUD_AUDIO=1 in the backend env to enable."
                    >
                      cloud
                    </span>
                  )}
                  {p.concurrency_safe && (
                    <span
                      className="rounded bg-emerald-900/40 px-1.5 py-0.5 text-[10px] text-emerald-300"
                      title="Multiple analyses can run in parallel against this plugin."
                    >
                      parallel-safe
                    </span>
                  )}
                </div>
                <div className="text-xs text-zinc-500">
                  {p.description || "—"}
                  <span className="ml-2 text-zinc-600">timeout {p.default_timeout_sec}s</span>
                </div>
              </div>
              <button
                onClick={() => pingPlugin(p.name)}
                className="rounded bg-zinc-700 px-3 py-1 text-xs hover:bg-zinc-600"
                title="Calls plugin.ping — every plugin supports this via the SDK"
              >
                ping
              </button>
            </li>
          ))}
        </ul>
        {pluginOutput && (
          <pre className="mt-3 overflow-x-auto rounded bg-black/50 p-3 text-xs text-zinc-300">
            {pluginOutput}
          </pre>
        )}
      </Section>

      <Section title="Tracks">
        <div className="flex gap-2">
          <input
            value={ingestPath}
            onChange={(e) => setIngestPath(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && ingest()}
            placeholder="absolute path to an audio file (or any file for now)"
            className="flex-1 rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm font-mono placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
          />
          <button
            onClick={ingest}
            className="rounded bg-blue-600 px-4 py-2 text-sm hover:bg-blue-500"
          >
            ingest
          </button>
        </div>
        {tracks.length === 0 ? (
          <p className="mt-3 text-sm text-zinc-500">none</p>
        ) : (
          <table className="mt-3 w-full text-xs font-mono">
            <thead className="text-left text-zinc-500">
              <tr>
                <th className="py-1 pr-3">hash</th>
                <th className="py-1 pr-3">format</th>
                <th className="py-1 pr-3">size</th>
                <th className="py-1">path</th>
              </tr>
            </thead>
            <tbody>
              {tracks.map((t) => (
                <tr key={t.content_hash} className="border-t border-zinc-800/50">
                  <td className="py-1 pr-3" title={t.content_hash}>
                    <Link
                      to={`/track/${t.content_hash}`}
                      className="text-purple-300 hover:text-purple-200"
                    >
                      {t.content_hash.slice(0, 12)}
                    </Link>
                  </td>
                  <td className="py-1 pr-3">{t.format ?? "—"}</td>
                  <td className="py-1 pr-3">{fmtBytes(t.file_size)}</td>
                  <td className="py-1 break-all text-zinc-400">{t.source_path}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>

      <Section title="Jobs">
        {jobs.length === 0 ? (
          <p className="text-sm text-zinc-500">none</p>
        ) : (
          <table className="w-full text-xs font-mono">
            <thead className="text-left text-zinc-500">
              <tr>
                <th className="py-1 pr-3">id</th>
                <th className="py-1 pr-3">kind</th>
                <th className="py-1 pr-3">status</th>
                <th className="py-1">retries</th>
              </tr>
            </thead>
            <tbody>
              {jobs.map((j) => (
                <tr key={j.id} className="border-t border-zinc-800/50">
                  <td className="py-1 pr-3">{j.id}</td>
                  <td className="py-1 pr-3">{j.kind}</td>
                  <td className="py-1 pr-3">
                    <StatusPill status={j.status} />
                  </td>
                  <td className="py-1">
                    {j.retries}/{j.max_retries}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Section>
    </div>
  );
}
