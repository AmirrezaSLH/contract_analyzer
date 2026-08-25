import { describe, expect, it } from "vitest";
import { lineSeries } from "../src/views/Monitor/charts";
import { monitorFixture } from "../src/views/Monitor/fixture";
import { exhaustedChip, fiveXxChip, stageChip, usedChip } from "../src/views/Monitor/status";

describe("host used-% chips", () => {
  it("is plenty under 20%, filling in the band, tight over 90%", () => {
    expect(usedChip(0.18)).toEqual({ state: "good", words: "plenty" });
    expect(usedChip(0.41)).toEqual({ state: "warn", words: "filling" });
    expect(usedChip(0.91)).toEqual({ state: "Non-Compliant", words: "tight" });
    expect(usedChip(null)).toBeNull();
  });
});

describe("reliability chips", () => {
  it("stays quiet under 1% 5xx and lights failing above it", () => {
    expect(fiveXxChip(0.003)).toBeNull();
    expect(fiveXxChip(0.02)).toEqual({ state: "Non-Compliant", words: "failing" });
  });

  it("does not call 1-of-1 a 100% stage failure", () => {
    expect(stageChip(1, 1)).toEqual({ state: "neutral", words: "not enough" });
    expect(stageChip(null, 0)).toBeNull();
    expect(stageChip(0.02, 40)).toBeNull();
    expect(stageChip(0.08, 40)).toEqual({ state: "Non-Compliant", words: "failing" });
  });

  it("treats exhausted retries the same as 5xx", () => {
    expect(exhaustedChip(0.002)).toBeNull();
    expect(exhaustedChip(0.02)?.words).toBe("failing");
  });
});

describe("fixture", () => {
  it("emits one bucket per window slot so a window change redraws the chart", () => {
    expect(monitorFixture("24h").series.httpRpm).toHaveLength(24);
    expect(monitorFixture("7d").series.httpRpm).toHaveLength(28);
    expect(monitorFixture("30d").series.disk).toHaveLength(30);
  });
});

describe("line series", () => {
  it("breaks the path at a null instead of drawing a zero", () => {
    const { series } = lineSeries(
      [
        { bucket: "2026-08-24T10:00:00Z", value: 10 },
        { bucket: "2026-08-24T11:00:00Z", value: null },
        { bucket: "2026-08-24T12:00:00Z", value: 12 },
      ],
      "24h",
      (value) => String(value),
    );
    expect(series.segments).toHaveLength(0);
    expect(series.points).toHaveLength(2);
  });

  it("draws one segment across consecutive measurements", () => {
    const { series } = lineSeries(
      [
        { bucket: "2026-08-24T10:00:00Z", value: 10 },
        { bucket: "2026-08-24T11:00:00Z", value: 12 },
      ],
      "24h",
      (value) => String(value),
    );
    expect(series.segments).toHaveLength(1);
    expect(series.points).toHaveLength(2);
  });
});
