/**
 * The home page: backend status, plugins list, ingest, tracks table, jobs.
 *
 * Track rows now link to /track/:hash, where the per-track view lives.
 */
import { useEffect, useReducer, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  api,
  type AnalysisLabelKind,
  type CandidateGraphBuildResult,
  type Health,
  type Job,
  type LabelRollup,
  type Plugin,
  type Project,
  type Track,
} from "../api";
import { Section, StatusPill } from "../components/ui";
import { fmtBytes } from "../lib/format";
import { LABEL_KINDS } from "../lib/labels";

export function LibraryPage() {
  const [state, dispatchLibrary] = useReducer(
    libraryReducer,
    INITIAL_LIBRARY_STATE,
  );
  const {
    health,
    healthErr,
    plugins,
    tracks,
    jobs,
    rollup,
    rollupErr,
    projects,
    graphResult,
    graphErr,
    graphBuilding,
    pluginOutput,
    ingestPath,
  } = state;

  // mountedRef + seqRef mirror the pattern in TrackDetailPage: drop responses
  // from in-flight requests after unmount, and from older refresh cycles that
  // resolve after a newer one already ran. Without this, a refresh started
  // before unmount can land state on a dead component, or a slow first refresh
  // can clobber a fresher second one.
  const mountedRef = useRef(true);
  const refreshSeqRef = useRef(0);

  async function refresh(signal?: AbortSignal) {
    const seq = ++refreshSeqRef.current;
    const opts = signal ? { signal } : undefined;
    const [
      healthResult,
      pluginsResult,
      tracksResult,
      jobsResult,
      rollupResult,
      projectsResult,
    ] =
      // Single-user polling is acceptable here. Before any multi-user/shared
      // deployment, split these probes by cadence or add backoff.
      await Promise.allSettled([
        api.health(opts),
        api.listPlugins(opts),
        api.listTracks(opts),
        api.listJobs(opts),
        api.getLabelRollup(opts),
        api.listProjects(opts),
      ]);
    if (
      !mountedRef.current ||
      seq !== refreshSeqRef.current ||
      (signal?.aborted ?? false)
    ) {
      return;
    }
    const patch: Partial<LibraryState> = {};

    if (healthResult.status === "fulfilled") {
      patch.health = healthResult.value;
      patch.healthErr = null;
    } else {
      // Clear stale healthy state — if the latest probe failed, the UI must
      // not keep displaying the *previous* successful response as if it were
      // current. The error message takes the slot instead.
      patch.health = null;
      patch.healthErr = errorMessage(healthResult.reason);
    }

    if (pluginsResult.status === "fulfilled") {
      patch.plugins = pluginsResult.value;
    } else {
      console.error("listPlugins failed", pluginsResult.reason);
    }

    if (tracksResult.status === "fulfilled") {
      patch.tracks = tracksResult.value;
    } else {
      console.error("listTracks failed", tracksResult.reason);
    }

    if (jobsResult.status === "fulfilled") {
      patch.jobs = jobsResult.value;
    } else {
      console.error("listJobs failed", jobsResult.reason);
    }

    if (rollupResult.status === "fulfilled") {
      patch.rollup = rollupResult.value;
      patch.rollupErr = null;
    } else {
      patch.rollupErr = errorMessage(rollupResult.reason);
      console.error("getLabelRollup failed", rollupResult.reason);
    }

    if (projectsResult.status === "fulfilled") {
      patch.projects = projectsResult.value;
    } else {
      console.error("listProjects failed", projectsResult.reason);
    }
    dispatchLibrary(patch);
  }

  useEffect(() => {
    mountedRef.current = true;
    const controller = new AbortController();
    refresh(controller.signal);
    const t = setInterval(() => refresh(controller.signal), 5000);
    return () => {
      mountedRef.current = false;
      controller.abort();
      clearInterval(t);
    };
  }, []);

  async function pingPlugin(name: string) {
    // ``ping`` is reserved by the SDK and supported by every plugin — safe to
    // call on any name without knowing what methods the plugin implements.
    try {
      const out = await api.callPlugin(name, "ping");
      dispatchLibrary({ pluginOutput: `${name}: ${JSON.stringify(out.result)}` });
    } catch (e) {
      dispatchLibrary({ pluginOutput: `${name}: error: ${errorMessage(e)}` });
    }
  }

  async function ingest() {
    if (!ingestPath.trim()) return;
    try {
      await api.ingestTrack(ingestPath.trim());
      dispatchLibrary({ ingestPath: "" });
      await refresh();
    } catch (e) {
      alert((e as Error).message);
    }
  }

  async function buildGraph() {
    dispatchLibrary({ graphBuilding: true, graphErr: null });
    try {
      const project =
        projects[0] ??
        (await api.createProject({
          name: `Truth test graph ${new Date().toLocaleString()}`,
          intent: "Phase 3 mechanical transition candidate graph",
        }));
      const result = await api.buildCandidateGraph(project.id, {
        force: true,
        max_candidates_per_pair: 3,
      });
      dispatchLibrary({
        graphResult: result,
        projects: projects.some((p) => p.id === project.id)
          ? projects
          : [project, ...projects],
      });
    } catch (e) {
      dispatchLibrary({ graphErr: errorMessage(e) });
    } finally {
      dispatchLibrary({ graphBuilding: false });
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
                type="button"
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
            onChange={(e) => dispatchLibrary({ ingestPath: e.target.value })}
            onKeyDown={(e) => e.key === "Enter" && ingest()}
            placeholder="absolute path to an audio file (or any file for now)"
            aria-label="Path to local audio file to ingest"
            className="flex-1 rounded border border-zinc-800 bg-zinc-900 px-3 py-2 text-sm font-mono placeholder-zinc-600 focus:border-zinc-600 focus:outline-none"
          />
          <button
            type="button"
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

      <Section title="Bake-off rollup">
        <RollupSection rollup={rollup} error={rollupErr} />
      </Section>

      <Section title="Transition graph">
        <GraphSection
          projects={projects}
          result={graphResult}
          error={graphErr}
          building={graphBuilding}
          onBuild={() => void buildGraph()}
        />
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

type LibraryState = {
  health: Health | null;
  healthErr: string | null;
  plugins: Plugin[];
  tracks: Track[];
  jobs: Job[];
  rollup: LabelRollup | null;
  rollupErr: string | null;
  projects: Project[];
  graphResult: CandidateGraphBuildResult | null;
  graphErr: string | null;
  graphBuilding: boolean;
  pluginOutput: string;
  ingestPath: string;
};

const INITIAL_LIBRARY_STATE: LibraryState = {
  health: null,
  healthErr: null,
  plugins: [],
  tracks: [],
  jobs: [],
  rollup: null,
  rollupErr: null,
  projects: [],
  graphResult: null,
  graphErr: null,
  graphBuilding: false,
  pluginOutput: "",
  ingestPath: "",
};

function libraryReducer(
  state: LibraryState,
  patch: Partial<LibraryState>,
): LibraryState {
  return { ...state, ...patch };
}

// ---------------------------------------------------------------------------
// Bake-off rollup — analyzer × label-kind matrix, optional per-genre breakdown
// ---------------------------------------------------------------------------

function errorMessage(value: unknown): string {
  return value instanceof Error ? value.message : String(value);
}

function GraphSection({
  projects,
  result,
  error,
  building,
  onBuild,
}: {
  projects: Project[];
  result: CandidateGraphBuildResult | null;
  error: string | null;
  building: boolean;
  onBuild: () => void;
}) {
  const latest = result?.project ?? projects[0] ?? null;
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="text-sm text-zinc-400">
          {latest ? (
            <>
              project{" "}
              <span className="font-mono text-zinc-200">
                #{latest.id} {latest.name}
              </span>
            </>
          ) : (
            "no graph project yet"
          )}
        </div>
        <button
          type="button"
          onClick={onBuild}
          disabled={building}
          className="rounded bg-blue-600 px-3 py-1.5 text-xs hover:bg-blue-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {building ? "building…" : "build graph"}
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
      {result ? (
        <div className="space-y-2">
          <div className="grid grid-cols-3 gap-2 text-xs font-mono text-zinc-400">
            <span>requested {result.requested_tracks}</span>
            <span>usable {result.usable_tracks}</span>
            <span>candidates {result.candidates.length}</span>
          </div>
          {result.warnings.map((warning) => (
            <p key={warning} className="text-xs text-amber-300">
              {warning}
            </p>
          ))}
          {Object.keys(result.skipped_tracks).length > 0 && (
            <p className="break-all text-xs text-zinc-500">
              skipped{" "}
              {Object.entries(result.skipped_tracks)
                .map(([hash, reason]) => `${hash.slice(0, 8)}:${reason}`)
                .join(", ")}
            </p>
          )}
          {result.candidates.length > 0 ? (
            <table className="w-full text-xs font-mono">
              <thead className="text-left text-zinc-500">
                <tr>
                  <th className="py-1 pr-3">edge</th>
                  <th className="py-1 pr-3">cue bars</th>
                  <th className="py-1 pr-3">score</th>
                  <th className="py-1 pr-3">tempo</th>
                  <th className="py-1">techniques</th>
                </tr>
              </thead>
              <tbody>
                {result.candidates.slice(0, 12).map((candidate) => (
                  <tr
                    key={
                      candidate.id ??
                      `${candidate.from_track}-${candidate.to_track}`
                    }
                  >
                    <td className="border-t border-zinc-800/50 py-1 pr-3">
                      {candidate.from_track.slice(0, 8)} →{" "}
                      {candidate.to_track.slice(0, 8)}
                    </td>
                    <td className="border-t border-zinc-800/50 py-1 pr-3">
                      {candidate.from_cue_bar} → {candidate.to_cue_bar}
                    </td>
                    <td className="border-t border-zinc-800/50 py-1 pr-3">
                      {candidate.scores.score.toFixed(3)}
                    </td>
                    <td className="border-t border-zinc-800/50 py-1 pr-3">
                      {candidate.scores.tempo_delta_pct.toFixed(1)}%
                    </td>
                    <td className="border-t border-zinc-800/50 py-1">
                      {candidate.allowed_techniques.join(", ")}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          ) : (
            <p className="text-sm text-zinc-500">
              no mechanically compatible edges yet
            </p>
          )}
        </div>
      ) : (
        <p className="text-sm text-zinc-500">
          builds phrase-aligned candidate edges from current TrackProfiles
        </p>
      )}
    </div>
  );
}

function RollupSection({
  rollup,
  error,
}: {
  rollup: LabelRollup | null;
  error: string | null;
}) {
  const [byGenre, setByGenre] = useState(false);

  if (!rollup) {
    return (
      <p className="text-sm text-zinc-500">
        {error ? `rollup unavailable: ${error}` : "loading…"}
      </p>
    );
  }
  if (rollup.total_labels === 0) {
    return (
      <div className="space-y-2">
        {error && <p className="text-xs text-amber-300">rollup refresh failed: {error}</p>}
        <p className="text-sm text-zinc-500">
          no labels yet — open a track, run analyzers, listen with the click track,
          and tag each run with what you hear
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      {error && <p className="text-xs text-amber-300">rollup refresh failed: {error}</p>}
      <div className="flex flex-wrap items-center gap-3 text-xs text-zinc-500">
        <span>
          {rollup.total_labels} labels across {rollup.total_labeled_runs} analysis runs
        </span>
        <button
          type="button"
          onClick={() => setByGenre((v) => !v)}
          className="rounded bg-zinc-800 px-2 py-0.5 hover:bg-zinc-700"
        >
          {byGenre ? "hide genre breakdown" : "show by genre"}
        </button>
      </div>
      {byGenre ? (
        <RollupTableByGenre data={rollup.by_analyzer_and_genre} />
      ) : (
        <RollupTable data={rollup.by_analyzer} />
      )}
    </div>
  );
}

type CountsByKind = Partial<Record<AnalysisLabelKind, number>>;

function rowTotal(counts: CountsByKind): number {
  return Object.values(counts).reduce<number>((s, n) => s + (n ?? 0), 0);
}

function HeaderRow({ extraCols = 0 }: { extraCols?: number }) {
  return (
    <thead className="text-left text-zinc-500">
      <tr>
        <th className="py-1 pr-3">analyzer</th>
        {extraCols > 0 && <th className="py-1 pr-3">genre</th>}
        {LABEL_KINDS.map((k) => (
          <th
            key={k.kind}
            className="px-2 py-1 text-center"
            title={k.kind}
          >
            {k.tag}
          </th>
        ))}
        <th className="py-1 pl-3 text-right">total</th>
      </tr>
    </thead>
  );
}

function CountCells({ counts }: { counts: CountsByKind }) {
  return (
    <>
      {LABEL_KINDS.map((k) => {
        const n = counts[k.kind] ?? 0;
        return (
          <td
            key={k.kind}
            className={`px-2 py-1 text-center ${
              n > 0 ? `rounded ${k.tone}` : "text-zinc-700"
            }`}
          >
            {n > 0 ? n : "·"}
          </td>
        );
      })}
    </>
  );
}

function RollupTable({ data }: { data: Record<string, CountsByKind> }) {
  const analyzers = Object.keys(data).sort();
  return (
    <table className="w-full text-xs font-mono">
      <HeaderRow />
      <tbody>
        {analyzers.map((a) => {
          const counts = data[a];
          return (
            <tr key={a} className="border-t border-zinc-800/50">
              <td className="py-1 pr-3 text-zinc-100">{a}</td>
              <CountCells counts={counts} />
              <td className="py-1 pl-3 text-right text-zinc-400">
                {rowTotal(counts)}
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function RollupTableByGenre({
  data,
}: {
  data: Record<string, Record<string, CountsByKind>>;
}) {
  const rows: { analyzer: string; genre: string; counts: CountsByKind }[] = [];
  for (const [analyzer, genres] of Object.entries(data)) {
    for (const [genre, counts] of Object.entries(genres)) {
      rows.push({ analyzer, genre, counts });
    }
  }
  rows.sort(
    (a, b) =>
      a.analyzer.localeCompare(b.analyzer) || a.genre.localeCompare(b.genre),
  );

  return (
    <table className="w-full text-xs font-mono">
      <HeaderRow extraCols={1} />
      <tbody>
        {rows.map(({ analyzer, genre, counts }) => (
          <tr
            key={`${analyzer}::${genre}`}
            className="border-t border-zinc-800/50"
          >
            <td className="py-1 pr-3 text-zinc-100">{analyzer}</td>
            <td className="py-1 pr-3 text-zinc-400">{genre}</td>
            <CountCells counts={counts} />
            <td className="py-1 pl-3 text-right text-zinc-400">
              {rowTotal(counts)}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
