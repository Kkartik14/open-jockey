import { useEffect, useState } from "react";
import { api, type Health, type Plugin, type Track, type Job } from "./api";

export default function App() {
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

  async function callEcho(name: string) {
    try {
      const out = await api.callPlugin(name, "echo", {
        from: "frontend",
        ts: new Date().toISOString(),
      });
      setPluginOutput(JSON.stringify(out.result, null, 2));
    } catch (e) {
      setPluginOutput(`error: ${(e as Error).message}`);
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
    <div className="min-h-screen w-full p-6 max-w-5xl mx-auto space-y-8 text-zinc-200">
      <header className="flex items-center justify-between border-b border-zinc-800 pb-4">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">aidj</h1>
          <p className="text-sm text-zinc-500">Phase 0 — foundation</p>
        </div>
        <HealthBadge health={health} err={healthErr} />
      </header>

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
                <div className="font-mono text-sm">
                  {p.name}
                  <span className="text-zinc-500"> @ {p.version}</span>
                </div>
                <div className="text-xs text-zinc-500">{p.description || "—"}</div>
              </div>
              <button
                onClick={() => callEcho(p.name)}
                className="rounded bg-zinc-700 px-3 py-1 text-xs hover:bg-zinc-600"
              >
                call echo()
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
                    {t.content_hash.slice(0, 12)}
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

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h2 className="mb-3 text-xs font-semibold uppercase tracking-wider text-zinc-500">
        {title}
      </h2>
      {children}
    </section>
  );
}

function HealthBadge({ health, err }: { health: Health | null; err: string | null }) {
  if (err) {
    return (
      <span className="rounded-full bg-red-900/40 px-3 py-1 text-xs font-mono text-red-300">
        backend offline
      </span>
    );
  }
  if (!health) {
    return (
      <span className="rounded-full bg-zinc-800 px-3 py-1 text-xs font-mono text-zinc-400">
        connecting…
      </span>
    );
  }
  return (
    <span className="rounded-full bg-green-900/40 px-3 py-1 text-xs font-mono text-green-300">
      backend ok · v{health.version}
    </span>
  );
}

function StatusPill({ status }: { status: string }) {
  const colours: Record<string, string> = {
    queued: "bg-yellow-900/40 text-yellow-300",
    running: "bg-blue-900/40 text-blue-300",
    completed: "bg-green-900/40 text-green-300",
    failed: "bg-red-900/40 text-red-300",
    cancelled: "bg-zinc-800 text-zinc-400",
  };
  return (
    <span className={`rounded px-2 py-0.5 text-[10px] ${colours[status] ?? "bg-zinc-800"}`}>
      {status}
    </span>
  );
}

function fmtBytes(n: number | null): string {
  if (n == null) return "—";
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}
