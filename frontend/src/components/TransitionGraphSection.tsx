import { useState } from "react";
import type {
  CandidateGraphBuildResult,
  Project,
  RenderTechnique,
  TransitionCandidate,
} from "../api";

type Props = {
  projects: Project[];
  result: CandidateGraphBuildResult | null;
  error: string | null;
  building: boolean;
  renderingCandidateIds: ReadonlySet<number>;
  onBuild: () => void;
  onRender: (candidate: TransitionCandidate, technique: RenderTechnique) => void;
};

export function TransitionGraphSection({
  projects,
  result,
  error,
  building,
  renderingCandidateIds,
  onBuild,
  onRender,
}: Props) {
  const latest = result?.project ?? projects[0] ?? null;
  const [selectedTechnique, setSelectedTechnique] = useState<Record<number, RenderTechnique>>({});

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
                  <th className="py-1 pr-3">verification</th>
                  <th className="py-1">render</th>
                </tr>
              </thead>
              <tbody>
                {result.candidates.slice(0, 12).map((candidate) => {
                  const candidateId = candidate.id;
                  const selected =
                    (candidateId && selectedTechnique[candidateId]) ??
                    candidate.allowed_techniques[0];
                  const rendering =
                    candidateId !== null && renderingCandidateIds.has(candidateId);
                  return (
                    <tr
                      key={
                        candidate.id ??
                        `${candidate.from_track}-${candidate.to_track}-${candidate.from_cue_bar}-${candidate.to_cue_bar}`
                      }
                    >
                      <td className="border-t border-zinc-800/50 py-1 pr-3">
                        {candidate.from_track.slice(0, 8)} -&gt;{" "}
                        {candidate.to_track.slice(0, 8)}
                      </td>
                      <td className="border-t border-zinc-800/50 py-1 pr-3">
                        {candidate.from_cue_bar} -&gt; {candidate.to_cue_bar}
                      </td>
                      <td className="border-t border-zinc-800/50 py-1 pr-3">
                        {candidate.scores.score.toFixed(3)}
                      </td>
                      <td className="border-t border-zinc-800/50 py-1 pr-3">
                        {candidate.scores.tempo_delta_pct.toFixed(1)}%
                      </td>
                      <td className="border-t border-zinc-800/50 py-1 pr-3">
                        {candidate.scores.verification}
                      </td>
                      <td className="border-t border-zinc-800/50 py-1">
                        <div className="flex flex-wrap items-center gap-2">
                          {candidate.allowed_techniques.length > 1 ? (
                            <select
                              value={selected}
                              onChange={(event) => {
                                if (candidateId === null) return;
                                setSelectedTechnique((current) => ({
                                  ...current,
                                  [candidateId]: event.target.value as RenderTechnique,
                                }));
                              }}
                              className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-xs text-zinc-100"
                              aria-label={`Technique for candidate ${candidateId ?? "unknown"}`}
                            >
                              {candidate.allowed_techniques.map((technique) => (
                                <option key={technique} value={technique}>
                                  {technique}
                                </option>
                              ))}
                            </select>
                          ) : (
                            <span>{selected ?? "-"}</span>
                          )}
                          <button
                            type="button"
                            disabled={candidateId === null || rendering || !selected}
                            onClick={() => {
                              if (selected) onRender(candidate, selected);
                            }}
                            className="rounded bg-emerald-700 px-2 py-1 text-xs hover:bg-emerald-600 disabled:cursor-not-allowed disabled:opacity-50"
                          >
                            {rendering ? "rendering…" : "render"}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
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
