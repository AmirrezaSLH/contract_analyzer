import { describe, expect, it } from "vitest";
import type { MetricsBucket, RunRow } from "../src/api/client";
import { barGeometry, costGeometry, lineGeometry, niceMax } from "../src/views/Metrics/charts";
import { ago, millis, percent, seconds, usd } from "../src/views/Metrics/format";
import { runOutcome } from "../src/views/Metrics/outcome";
import {
  costTailRatio,
  meterFill,
  meterTick,
  statusOf,
  THRESHOLDS,
} from "../src/views/Metrics/thresholds";

/**
 * The KPI page's arithmetic, tested where it can be wrong.
 *
 * Everything here is a trap `03_data_contract.md` names, and each one fails
 * *silently* if it is got wrong: a null coerced to zero draws a plausible
 * chart, a rate with no denominator reads as a healthy 0%, and a partial
 * bucket looks like a collapse. None of them throws, which is exactly why
 * they are worth a test rather than a fix.
 */

const bucket = (over: Partial<MetricsBucket> = {}): MetricsBucket => ({
  bucket: "2026-08-24T12:00:00Z",
  runs: 0,
  done: 0,
  failed: 0,
  cost_usd: 0,
  job_duration_s: { p50: null, p95: null },
  cost_percentiles: { p50: null, p95: null },
  mean_confidence: null,
  quote_verification_rate: null,
  needs_review_rate: null,
  states: {},
  chat: { turns: 0, cost_usd: 0 },
  ...over,
});

const run = (over: Partial<RunRow> = {}): RunRow => ({
  analysis_id: "a1",
  trace_id: "7071f7db-3913c303",
  document_id: 1,
  filename: "Sample Contract.pdf",
  surface: "api",
  status: "done",
  criteria_requested: 5,
  criteria_completed: 5,
  criteria_skipped: 0,
  error: null,
  created_at: "2026-08-24T18:40:31+00:00",
  started_at: null,
  completed_at: null,
  job_duration_s: 41.99,
  cost_usd: 0.8,
  input_tokens: 0,
  output_tokens: 0,
  tool_calls: 0,
  needs_review: 0,
  capped: 0,
  mean_confidence: 0.83,
  quotes_total: 17,
  quotes_verified: 17,
  ...over,
});

describe("units", () => {
  it("keeps seconds and milliseconds apart", () => {
    // Trap 6: `job_duration_s` and `chat.latency_ms` sit in the same payload, and
    // the conversion happens in exactly one place.
    expect(seconds(104.4)).toBe("104 s");
    // A chat p50 of 3120 *ms* is 3.1 s. Read as seconds it would be 52 minutes.
    expect(millis(3120)).toBe("3.1 s");
    expect(seconds(3120)).toBe("3120 s");
  });

  it("renders an absent number as a dash, never as zero", () => {
    // Trap 2. A `null` rate has no denominator; a `0%` claims it was measured.
    expect(percent(null)).toBe("—");
    expect(seconds(null)).toBe("—");
    expect(usd(null)).toBe("—");
    expect(percent(0)).toBe("0%");
  });

  it("counts back from the last refresh in the units the poll runs at", () => {
    const now = Date.parse("2026-08-24T18:00:00Z");
    expect(ago(now - 4_000, now)).toBe("4 s ago");
    expect(ago(now - 120_000, now)).toBe("2 m ago");
  });
});

describe("thresholds", () => {
  it("passes at the bound, in both directions", () => {
    // The real database sits exactly on this one: needs_review_rate 0.1
    // against a <= 10% target. Off-by-one here turns a healthy deployment
    // amber on the demo machine.
    expect(statusOf(0.1, THRESHOLDS.needsReview).tone).toBe("good");
    expect(statusOf(0.1001, THRESHOLDS.needsReview).tone).toBe("warn");
    expect(statusOf(0.99, THRESHOLDS.quoteVerification).tone).toBe("good");
    expect(statusOf(0.98, THRESHOLDS.quoteVerification).words).toBe("below target");
    expect(statusOf(0.11, THRESHOLDS.needsReview).words).toBe("over target");
  });

  it("calls an unmeasured rate unmeasured, not healthy", () => {
    // A green chip over a null is a lie: the rate was never measured.
    expect(statusOf(null, THRESHOLDS.quoteVerification)).toEqual({
      tone: "neutral",
      words: "not measured",
    });
  });

  it("puts the tick where a ceiling threshold can still be overshot", () => {
    // A 10% ceiling drawn on a 0-1 bar is a tick at the far left with no room
    // to show a breach. The track tops out above the limit instead.
    expect(meterTick(THRESHOLDS.needsReview)).toBeCloseTo(2 / 3, 5);
    expect(meterFill(0.15, THRESHOLDS.needsReview)).toBeCloseTo(1, 5);
    // A floor threshold keeps its natural scale: 99% is 99% along the bar.
    expect(meterTick(THRESHOLDS.quoteVerification)).toBeCloseTo(0.99, 5);
  });
});

describe("run outcome", () => {
  it("keeps needs-review out of the failure outcomes", () => {
    // Three outcomes, not two: this run succeeded and still wants a human.
    expect(runOutcome(run({ needs_review: 2 }))).toEqual({ tone: "warn", words: "2 need review" });
    expect(runOutcome(run({ needs_review: 1 })).words).toBe("1 needs review");
    expect(runOutcome(run()).words).toBe("5 of 5 complete");
  });

  it("keeps interrupted apart from failed", () => {
    // `interrupted` is a process that went away; `failed` is a run that broke.
    expect(runOutcome(run({ status: "failed" })).words).toBe("failed");
    expect(runOutcome(run({ status: "interrupted" })).words).toBe("interrupted");
    expect(runOutcome(run({ status: "cancelled" })).tone).toBe("neutral");
  });

  it("renders a status it has never seen rather than a blank cell", () => {
    expect(runOutcome(run({ status: "reconciling" })).words).toBe("reconciling");
  });
});

describe("the bar chart", () => {
  const buckets = [
    bucket({ bucket: "2026-08-24T10:00:00Z", runs: 4 }),
    bucket({ bucket: "2026-08-24T11:00:00Z", runs: 0 }),
    bucket({ bucket: "2026-08-24T12:00:00Z", runs: 1 }),
  ];

  it("draws a stub for an empty bucket rather than nothing", () => {
    // The axis has to show the quiet hours, or a busy night reads continuous.
    const { bars } = barGeometry(buckets, "24h");
    expect(bars[1]!.empty).toBe(true);
    expect(bars[1]!.height).toBe(2);
    expect(bars[1]!.title).toContain("no runs");
  });

  it("marks the last bucket as the partial one it is", () => {
    // Trap 8. Its bar is always short because the hour is not over.
    const { bars } = barGeometry(buckets, "24h");
    expect(bars[2]!.partial).toBe(true);
    expect(bars[2]!.title).toContain("so far");
    expect(bars[0]!.partial).toBe(false);
  });

  it("scales to a whole-numbered axis", () => {
    // A runs axis whose middle gridline reads 1.5 is labelling something that
    // cannot happen.
    expect(barGeometry(buckets, "24h").top).toBe(4);
    expect(niceMax(3, { integerHalves: true })).toBe(4);
    expect(niceMax(10, { integerHalves: true })).toBe(10);
    expect(niceMax(0, { integerHalves: true })).toBe(2);
  });
});

describe("the job duration chart", () => {
  const buckets = [
    bucket({ bucket: "2026-08-24T10:00:00Z", runs: 2, job_duration_s: { p50: 60, p95: 100 } }),
    bucket({ bucket: "2026-08-24T11:00:00Z", runs: 0 }),
    bucket({ bucket: "2026-08-24T12:00:00Z", runs: 1, job_duration_s: { p50: 58, p95: 104 } }),
  ];

  it("breaks the line at a bucket that measured nothing", () => {
    // Trap 1, and the one that matters most: a null coerced to zero draws a
    // cliff to the axis on every quiet hour and makes p95 look catastrophic.
    const { series } = lineGeometry(buckets, "24h");
    const p95 = series.find((entry) => entry.key === "p95")!;
    expect(p95.points).toHaveLength(2);
    // Two measured points either side of a gap are two segments of one point
    // each -- which draw nothing, and are carried by their dots.
    expect(p95.segments).toHaveLength(0);
  });

  it("joins consecutive measured points into one path", () => {
    const joined = [buckets[0]!, buckets[2]!];
    const { series } = lineGeometry(joined, "24h");
    const p50 = series.find((entry) => entry.key === "p50")!;
    expect(p50.segments).toHaveLength(1);
    expect(p50.segments[0]!.startsWith("M")).toBe(true);
  });

  it("puts p50 and p95 on one axis, topped just above the tail", () => {
    // Never a second y-scale: they share a unit, so they share a scale. 104 s
    // tops out at 120 rather than 200, which is the axis the design draws.
    const { top } = lineGeometry(buckets, "24h");
    expect(top).toBe(120);
  });

  it("direct-labels each series at its last measured point", () => {
    // --chart-2 is 2.94:1 on white; the legend alone is not enough.
    const { series } = lineGeometry(buckets, "24h");
    for (const entry of series) {
      expect(entry.label_at).not.toBeNull();
      expect(entry.label_at!.key).toBe("2026-08-24T12:00:00Z");
    }
  });

  it("has nothing to draw when no bucket measured anything", () => {
    const { series } = lineGeometry([bucket(), bucket({ bucket: "2026-08-24T13:00:00Z" })], "24h");
    expect(series.every((entry) => entry.points.length === 0)).toBe(true);
    expect(series.every((entry) => entry.segments.length === 0)).toBe(true);
  });
});

describe("cost", () => {
  const buckets = [
    bucket({ bucket: "2026-08-24T10:00:00Z", runs: 2, cost_usd: 1.7 }),
    bucket({ bucket: "2026-08-24T11:00:00Z", runs: 0, cost_usd: 0 }),
    bucket({ bucket: "2026-08-24T12:00:00Z", runs: 1, cost_usd: 0.81 }),
  ];

  it("gives spend an axis of its own, in dollars", () => {
    // Never a second y-scale: runs and cost share no unit, so they share no
    // axis. Two quantities wanted together is two charts.
    const runsAxis = barGeometry(buckets, "24h");
    const costAxis = costGeometry(buckets, "24h");
    expect(runsAxis.top).toBe(2);
    expect(costAxis.top).toBeGreaterThan(1.7);
    expect(costAxis.grid.map((line) => line.label)).toContain("$0.00");
    expect(runsAxis.grid.map((line) => line.label)).toContain("0");
  });

  it("says a bucket spent nothing rather than drawing nothing", () => {
    const { bars } = costGeometry(buckets, "24h");
    expect(bars[1]!.empty).toBe(true);
    expect(bars[1]!.title).toContain("nothing spent");
    expect(bars[0]!.title).toContain("$1.70");
  });

  it("reads the tail as a ratio, so the tripwire survives a corpus change", () => {
    // p95 <= ~2x p50. A fixed dollar cap would need re-setting every time the
    // contracts got longer; a ratio does not.
    expect(costTailRatio(0.8, 1.1)).toBeCloseTo(1.375, 3);
    expect(statusOf(costTailRatio(0.8, 1.1), THRESHOLDS.costTail).tone).toBe("good");
    expect(statusOf(costTailRatio(0.5, 1.4), THRESHOLDS.costTail).tone).toBe("warn");
  });

  it("refuses to divide by an empty window", () => {
    // Without the guard a window with no spend reports Infinity, which reads
    // as a breach on a deployment that has not spent a cent.
    expect(costTailRatio(0, 0)).toBeNull();
    expect(costTailRatio(null, 1.1)).toBeNull();
    expect(statusOf(costTailRatio(0, 0), THRESHOLDS.costTail).words).toBe("not measured");
  });

  it("prints a per-run cost at the precision a demo is judged on", () => {
    // ~$0.96 measured per run. `$1` would lose the whole cost/quality story.
    expect(usd(0.844401)).toBe("$0.84");
    expect(usd(16.8)).toBe("$16.80");
  });
});
