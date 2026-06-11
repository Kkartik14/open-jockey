import { describe, expect, it } from "vitest";
import { LABEL_KINDS } from "./labels";

describe("LABEL_KINDS", () => {
  it("matches the backend AnalysisLabelKind contract in canonical display order", () => {
    const kinds = LABEL_KINDS.map((item) => item.kind);

    expect(kinds).toEqual([
      "correct",
      "half_time",
      "double_time",
      "wrong_downbeat_phase",
      "early_by_ms",
      "late_by_ms",
      "wrong_section_labels",
      "unusable",
    ]);
    expect(new Set(kinds).size).toBe(kinds.length);
  });
});
