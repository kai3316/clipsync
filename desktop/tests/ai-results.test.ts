import { describe, expect, it } from "vitest";
import { requireAiPull, requireAiSuccess } from "../src/api/bridge";

describe("AI business results", () => {
  it("rejects offline, empty and malformed pull acknowledgments", () => {
    for (const result of [
      { requested: 0, errors: ["peer_offline"] },
      { requested: 0, errors: [] }, {}, { requested: -1 },
      { requested: "1" }, { requested: Number.NaN },
    ]) expect(() => requireAiPull(result)).toThrow();
  });
  it("preserves partial pull errors instead of treating acceptance as delivery", () => {
    const result = { requested: 1, errors: ["send_failed"], expanded: 2 };
    expect(requireAiPull(result)).toBe(result);
  });
  it("rejects failed reads, saves and trash operations instead of reporting success", () => {
    for (const error of ["binary", "not_found", "io_error", "invalid_item"]) {
      expect(() => requireAiSuccess({ ok: false, error })).toThrow();
    }
  });
  it("preserves empty file content, truncation flags and inventory responses", () => {
    for (const result of [
      { ok: true, content: "", truncated: false },
      { ok: true, content: "partial", truncated: true },
      { entries: [] },
    ]) {
      expect(requireAiSuccess(result)).toBe(result);
    }
  });
});
