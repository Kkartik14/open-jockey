import { api, type RenderArtifact, type RenderLabel, type RenderLabelKind } from "../api";
import { StatusPill } from "./ui";

type Props = {
  renders: RenderArtifact[];
  labelsByRender: Record<number, RenderLabel[]>;
  error: string | null;
  actionError: string | null;
  onRefresh: () => void;
  onCancel: (render: RenderArtifact) => void;
  onDelete: (render: RenderArtifact) => void;
  onAddLabel: (render: RenderArtifact, kind: RenderLabelKind) => void;
  onDeleteLabel: (render: RenderArtifact, label: RenderLabel) => void;
};

const RENDER_LABELS: { kind: RenderLabelKind; label: string }[] = [
  { kind: "good", label: "good" },
  { kind: "off_beat", label: "off beat" },
  { kind: "bad_cue", label: "bad cue" },
  { kind: "bad_energy", label: "bad energy" },
  { kind: "bad_key", label: "bad key" },
  { kind: "clipping", label: "clipping" },
  { kind: "wrong_tempo_match", label: "wrong tempo" },
  { kind: "too_abrupt", label: "too abrupt" },
  { kind: "too_long", label: "too long" },
  { kind: "boring", label: "boring" },
  { kind: "unusable", label: "unusable" },
];

export function RenderPanel({
  renders,
  labelsByRender,
  error,
  actionError,
  onRefresh,
  onCancel,
  onDelete,
  onAddLabel,
  onDeleteLabel,
}: Props) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <div className="text-sm text-zinc-400">{renders.length} render artifacts</div>
        <button
          type="button"
          onClick={onRefresh}
          className="rounded bg-zinc-800 px-3 py-1.5 text-xs hover:bg-zinc-700"
        >
          refresh
        </button>
      </div>
      {error && <p className="text-xs text-red-400">{error}</p>}
      {actionError && <p className="text-xs text-amber-300">{actionError}</p>}
      {renders.length === 0 ? (
        <p className="text-sm text-zinc-500">none</p>
      ) : (
        <div className="space-y-4">
          {renders.map((render) => (
            <RenderRow
              key={render.id}
              render={render}
              labels={labelsByRender[render.id] ?? []}
              onCancel={onCancel}
              onDelete={onDelete}
              onAddLabel={onAddLabel}
              onDeleteLabel={onDeleteLabel}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function RenderRow({
  render,
  labels,
  onCancel,
  onDelete,
  onAddLabel,
  onDeleteLabel,
}: {
  render: RenderArtifact;
  labels: RenderLabel[];
  onCancel: (render: RenderArtifact) => void;
  onDelete: (render: RenderArtifact) => void;
  onAddLabel: (render: RenderArtifact, kind: RenderLabelKind) => void;
  onDeleteLabel: (render: RenderArtifact, label: RenderLabel) => void;
}) {
  const audioSrc = api.renderAudioUrl(render.id);

  return (
    <article className="space-y-3 border-t border-zinc-800/70 pt-3">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
            <span>render #{render.id}</span>
            <StatusPill status={render.status} />
            <span className="text-zinc-400">{render.technique}</span>
            <span className="text-zinc-500">candidate {render.candidate_id}</span>
          </div>
          <div className="break-all font-mono text-xs text-zinc-500">
            {render.from_track.slice(0, 10)} -&gt; {render.to_track.slice(0, 10)}
          </div>
        </div>
        <div className="flex flex-wrap gap-2">
          {(render.status === "queued" || render.status === "running") && (
            <button
              type="button"
              onClick={() => onCancel(render)}
              className="rounded bg-zinc-700 px-2 py-1 text-xs hover:bg-zinc-600"
            >
              cancel
            </button>
          )}
          <button
            type="button"
            onClick={() => onDelete(render)}
            className="rounded bg-red-900/70 px-2 py-1 text-xs text-red-100 hover:bg-red-800"
          >
            delete
          </button>
        </div>
      </div>

      {render.status === "completed" && (
        <audio
          controls
          src={audioSrc}
          className="w-full"
          aria-label={`Render ${render.id} audio`}
        >
          <track kind="captions" src="data:text/vtt,WEBVTT%0A" label="captions" />
        </audio>
      )}

      <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs font-mono text-zinc-400 md:grid-cols-4">
        <dt>duration</dt>
        <dd>{fmtSec(render.duration_sec)}</dd>
        <dt>tempo ratio</dt>
        <dd>{render.request_config.tempo_match_ratio.toFixed(4)}</dd>
        <dt>source LUFS</dt>
        <dd>{fmtDb(render.actuals?.source_lufs ?? null)}</dd>
        <dt>target LUFS</dt>
        <dd>{fmtDb(render.actuals?.target_lufs ?? null)}</dd>
      </dl>

      {render.error && <p className="text-xs text-red-300">{render.error}</p>}
      {render.warnings.length > 0 && (
        <ul className="space-y-1 text-xs text-amber-300">
          {render.warnings.map((warning) => (
            <li key={warning}>{warning}</li>
          ))}
        </ul>
      )}

      <div className="space-y-2">
        <div className="flex flex-wrap gap-2">
          {RENDER_LABELS.map((label) => (
            <button
              key={label.kind}
              type="button"
              onClick={() => onAddLabel(render, label.kind)}
              className={`rounded px-2 py-1 text-xs ${
                label.kind === "good"
                  ? "bg-emerald-900/70 text-emerald-100 hover:bg-emerald-800"
                  : "bg-zinc-800 text-zinc-200 hover:bg-zinc-700"
              }`}
            >
              {label.label}
            </button>
          ))}
        </div>
        {labels.length > 0 && (
          <div className="flex flex-wrap gap-2">
            {labels.map((label) => (
              <button
                key={label.id}
                type="button"
                onClick={() => onDeleteLabel(render, label)}
                className="rounded bg-zinc-900 px-2 py-1 text-xs text-zinc-300 hover:bg-zinc-800"
                title={label.notes ?? label.kind}
              >
                {label.kind} x
              </button>
            ))}
          </div>
        )}
      </div>
    </article>
  );
}

function fmtSec(value: number | null): string {
  return value == null ? "-" : `${value.toFixed(1)}s`;
}

function fmtDb(value: number | null): string {
  return value == null ? "-" : `${value.toFixed(1)}`;
}
