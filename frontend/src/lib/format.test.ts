import { describe, expect, it } from "vitest";
import { fmtBytes } from "./format";

describe("fmtBytes", () => {
  it("keeps byte formatting stable for track tables", () => {
    expect(fmtBytes(null)).toBe("—");
    expect(fmtBytes(0)).toBe("0 B");
    expect(fmtBytes(512)).toBe("512 B");
    expect(fmtBytes(1024)).toBe("1.0 KB");
    expect(fmtBytes(1024 * 1024)).toBe("1.0 MB");
    expect(fmtBytes(1024 * 1024 * 1024)).toBe("1.00 GB");
  });
});
