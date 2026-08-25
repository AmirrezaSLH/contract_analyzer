import type { MonitorSnapshot, MonitorWindow, Sample } from "./types";

/**
 * Numbers for the Monitor page until `GET /monitor/*` exists.
 *
 * The shape is the contract the view is written against. Wiring the client
 * later is a swap of this function for a query; the tiles and charts do not
 * change. Values are plausible for a quiet demo box, not a recording.
 */

const ANCHOR = Date.parse("2026-08-24T18:00:00.000Z");

const COUNTS: Record<MonitorWindow, number> = { "30m": 60, "1h": 120 };
const STEPS_MS: Record<MonitorWindow, number> = {
  "30m": 30_000,
  "1h": 30_000,
};

export function monitorFixture(window: MonitorWindow): MonitorSnapshot {
  const buckets = timestamps(window);
  return {
    http: { rpm: 12.4, fiveXx: 0.003, p95Ms: 180 },
    upstream: { retriesPer100: 4.2, exhaustedRate: 0.002, topReason: "429" },
    stages: { name: "ingest.parse", errorRate: 0.02, p95S: 0.84, n: 40 },
    host: {
      rssPct: 0.18,
      rssMb: 360,
      diskPct: 0.41,
      diskGb: 16.4,
      diskTotalGb: 40,
    },
    series: {
      httpRpm: wave(buckets, 11, 3, 0),
      httpFiveXx: wave(buckets, 0.004, 0.003, 1, { floor: 0, ceil: 0.02 }),
      httpP95: wave(buckets, 160, 50, 2),
      retries: wave(buckets, 4, 2, 3),
      exhausted: wave(buckets, 0.002, 0.002, 4, { floor: 0, ceil: 0.015 }),
      stageError: wave(buckets, 0.018, 0.012, 5, { floor: 0, ceil: 0.08 }),
      stageP95: wave(buckets, 0.8, 0.25, 6),
      rss: wave(buckets, 0.17, 0.03, 7, { floor: 0.08, ceil: 0.45 }),
      disk: climb(buckets, 0.38, 0.04),
    },
  };
}

function timestamps(window: MonitorWindow): string[] {
  const count = COUNTS[window];
  const step = STEPS_MS[window];
  const start = ANCHOR - (count - 1) * step;
  return Array.from({ length: count }, (_, index) =>
    new Date(start + index * step).toISOString(),
  );
}

function wave(
  buckets: string[],
  mid: number,
  amp: number,
  phase: number,
  bounds?: { floor: number; ceil: number },
): Sample[] {
  return buckets.map((bucket, index) => {
    const raw = mid + amp * Math.sin((index + phase) / 3);
    const value = bounds
      ? Math.min(bounds.ceil, Math.max(bounds.floor, raw))
      : Math.max(0, raw);
    return { bucket, value };
  });
}

function climb(buckets: string[], start: number, rise: number): Sample[] {
  const last = Math.max(buckets.length - 1, 1);
  return buckets.map((bucket, index) => ({
    bucket,
    value: start + (rise * index) / last,
  }));
}
