import type { MonitorSnapshot, MonitorWindow, Sample } from "./types";

/**
 * Chart-length fixture for tests. Live tiles come from `GET /monitor/upstream`.
 *
 * Stages and host are live. Values here are plausible for a quiet demo box,
 * not a recording.
 */

const ANCHOR = Date.parse("2026-08-24T18:00:00.000Z");

const COUNTS: Record<MonitorWindow, number> = {
  "30m": 60,
  "1h": 120,
  "24h": 25,
  "7d": 29,
  "30d": 31,
};
const STEPS_MS: Record<MonitorWindow, number> = {
  "30m": 30_000,
  "1h": 30_000,
  "24h": 3_600_000,
  "7d": 6 * 3_600_000,
  "30d": 86_400_000,
};

export function monitorFixture(window: MonitorWindow): MonitorSnapshot {
  const buckets = timestamps(window);
  return {
    upstream: { retriesPer100: 4.2, exhaustedRate: 0.002, topReason: "429" },
    series: {
      retries: wave(buckets, 4, 2, 3),
      exhausted: wave(buckets, 0.002, 0.002, 4, { floor: 0, ceil: 0.015 }),
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
