import { LABEL_Y, VIEW, niceMax, type Gridline, type Point, type Tick } from "../Metrics/charts";
import { bucketLabel } from "../Metrics/format";
import type { Sample } from "./types";

/**
 * One series on the KPI line chart's geometry, without KPI bucket types.
 *
 * Trap 1 from `charts.ts` still holds: a `null` ends the path. Empty arrays
 * produce no marks. The last point is the bucket in progress.
 */

const LEFT = 30;
const RIGHT = 434;
const TOP = 12;
const BASE = 118;
const MAX_TICKS = 5;

export interface LineSeries {
  segments: string[];
  points: Point[];
}

export function lineSeries(
  samples: Sample[],
  window: string,
  words: (value: number) => string,
): { series: LineSeries; grid: Gridline[]; ticks: Tick[] } {
  const measured = samples
    .map((sample) => sample.value)
    .filter((value): value is number => value !== null && value !== undefined);
  const top = niceMax(Math.max(0, ...measured));
  const slot = samples.length ? (RIGHT - LEFT) / samples.length : 0;
  const width = Math.min(slot * 0.64, 20);

  const points: (Point | null)[] = samples.map((sample, index) => {
    const value = sample.value;
    if (value === null || value === undefined) return null;
    const partial = index === samples.length - 1;
    return {
      x: LEFT + index * slot + slot / 2,
      y: BASE - ((BASE - TOP) * value) / top,
      key: sample.bucket,
      title: `${bucketLabel(sample.bucket, window)} — ${words(value)}${partial ? " so far" : ""}`,
    };
  });
  const drawn = points.filter((point): point is Point => point !== null);

  return {
    series: { segments: segmentsOf(points), points: drawn },
    grid: [0, top / 2, top].map((value) => ({
      y: BASE - ((BASE - TOP) * value) / top,
      label: words(value),
    })),
    ticks: xTicks(samples, slot, width, window),
  };
}

function segmentsOf(points: (Point | null)[]): string[] {
  const paths: string[] = [];
  let run: Point[] = [];
  const flush = () => {
    if (run.length > 1) paths.push(run.map((p, i) => `${i ? "L" : "M"}${p.x},${p.y}`).join(" "));
    run = [];
  };
  for (const point of points) {
    if (point === null) flush();
    else run.push(point);
  }
  flush();
  return paths;
}

function xTicks(samples: Sample[], slot: number, width: number, window: string): Tick[] {
  const last = samples[samples.length - 1];
  if (last === undefined) return [];
  const step = Math.max(1, Math.ceil(samples.length / MAX_TICKS));
  const out: Tick[] = [];
  for (let index = 0; index < samples.length; index += step) {
    const sample = samples[index];
    if (sample === undefined) continue;
    out.push({
      x: LEFT + index * slot + slot / 2,
      label: bucketLabel(sample.bucket, window, { short: true }),
    });
  }
  const lastX = LEFT + (samples.length - 1) * slot + slot / 2;
  const previous = out[out.length - 1];
  if (previous !== undefined && lastX - previous.x > slot + width) {
    out.push({ x: lastX, label: bucketLabel(last.bucket, window, { short: true }) });
  }
  return out;
}

export { LABEL_Y, VIEW };
