/**
 * Shared definition of the verification-label kinds + their display tags.
 *
 * Used by the per-run LabelRow on the track-detail page and by the cross-track
 * Bake-off rollup table on the library page. The order here is the canonical
 * left-to-right order both views render.
 */
import type { AnalysisLabelKind } from "../api";

export interface LabelKindMeta {
  kind: AnalysisLabelKind;
  /** Short glyph/word shown in cells and on label-row buttons. */
  tag: string;
  /** Tailwind classes (bg + text) applied when the label is present. */
  tone: string;
}

export const LABEL_KINDS: ReadonlyArray<LabelKindMeta> = [
  { kind: "correct", tag: "✓", tone: "bg-emerald-900/40 text-emerald-300" },
  { kind: "half_time", tag: "½×", tone: "bg-amber-900/40 text-amber-300" },
  { kind: "double_time", tag: "2×", tone: "bg-amber-900/40 text-amber-300" },
  { kind: "wrong_downbeat_phase", tag: "phase", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "early_by_ms", tag: "early", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "late_by_ms", tag: "late", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "wrong_section_labels", tag: "sections", tone: "bg-rose-900/40 text-rose-300" },
  { kind: "unusable", tag: "unusable", tone: "bg-red-900/40 text-red-300" },
];
