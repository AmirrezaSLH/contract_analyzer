import type { MetricsBucket } from "../../api/client";
import { bucketLabel, plural, seconds, usd } from "./format";

/**
 * The geometry behind the two trend charts, as arithmetic with no React in it.
 *
 * Both charts are hand-drawn SVG. Two forms -- a bar chart and a two-series
 * line -- are less code than configuring a library, and the bundle stays as it
 * is. Keeping the arithmetic here is what makes the three traps below testable
 * rather than eyeballed.
 *
 * **Trap 1: an empty bucket has `runs: 0` but `latency_s.p50: null`.** They
 * are not the same absence. A zero run count is a fact -- the axis must show
 * the quiet hours -- so an empty bucket draws a 2px baseline stub rather than
 * nothing. A null percentile is *no measurement*, and a line chart that
 * coerces it to `0` draws a cliff to the axis on every quiet hour and makes
 * p95 look catastrophic. So: bars zero, lines break.
 *
 * **Trap 2: the last bucket is the current one and is partial.** A 24 h window
 * ends with the hour in progress, so its bar is always short and its
 * percentiles are computed over a fraction of an hour. It is marked -- a
 * lighter fill and `so far` in its tooltip -- rather than dropped, because
 * dropping it would hide the present.
 *
 * **Trap 3: one axis per chart, always.** p50 and p95 share a scale because
 * they share a unit. There is no second y-scale here and there is not going to
 * be one: if runs and cost are ever wanted together, that is two charts.
 */

/** The drawing box. A viewBox, not pixels: the SVG is `width: 100%` and the
 *  marks scale with the card. */
export const VIEW = { width: 440, height: 140 } as const;

/** The plot area inside it. The left gutter is the y labels; the bottom strip
 *  is the x labels. */
const LEFT = 30;
const RIGHT = 434;
const TOP = 12;
const BASE = 118;
export const LABEL_Y = 134;

/** At most this many x labels, however many buckets there are. 31 daily labels
 *  on a 404-unit axis is a smear. */
const MAX_TICKS = 5;

export interface Gridline {
  y: number;
  label: string;
}

export interface Tick {
  x: number;
  label: string;
}

export interface Bar {
  key: string;
  x: number;
  y: number;
  width: number;
  height: number;
  /** No runs in this bucket: the 2px stub, drawn in `--chart-empty`. */
  empty: boolean;
  /** The current, incomplete bucket. Lighter fill, and its tooltip says so. */
  partial: boolean;
  title: string;
}

export interface Point {
  x: number;
  y: number;
  key: string;
  title: string;
}

export interface Series {
  key: "p50" | "p95";
  label: string;
  /** One `d` per unbroken stretch. **A gap is a new segment, never a straight
   *  line across the missing points.** */
  segments: string[];
  points: Point[];
  /** Where the direct label goes: the last point drawn. `--chart-2` is 2.94:1
   *  on white, so the legend alone is not enough to tell the two apart. */
  label_at: Point | null;
}

interface BarOptions {
  /** What to draw. Zero is a real answer here -- it means the bucket happened
   *  and nothing was spent in it -- which is why bars zero and lines break. */
  value: (bucket: MetricsBucket) => number;
  /** The mark's tooltip, given the value. */
  words: (value: number, bucket: MetricsBucket) => string;
  /** The y-axis label. */
  axis: (value: number) => string;
  /** Whether the axis has to land on whole numbers. A count, yes; dollars, no. */
  integerHalves?: boolean;
}

/**
 * Bars over one value per bucket, **single hue, never stacked**.
 *
 * Completed-vs-failed as green and red separates at dE 4.9 for deuteranopia --
 * the classic red-green failure, well under the dE 8 floor -- so failures live
 * in the failure-rate tile with a chip that carries words, and a bar chart
 * here answers one question only.
 *
 * It is one function because runs-per-bucket and spend-per-bucket are the same
 * chart with a different accessor, and **they still get an axis each**: runs
 * and cost share no scale, and if they are ever wanted together that is two
 * charts, which is exactly what this is.
 */
export function barGeometry(buckets: MetricsBucket[], window: string, options?: BarOptions) {
  const opts: BarOptions = options ?? {
    value: (bucket) => bucket.runs,
    words: (value) => (value === 0 ? "no runs" : plural(value, "run")),
    axis: (value) => String(value),
    integerHalves: true,
  };
  const top = niceMax(Math.max(0, ...buckets.map(opts.value)), {
    integerHalves: opts.integerHalves,
  });
  const slot = buckets.length ? (RIGHT - LEFT) / buckets.length : 0;
  const width = Math.min(slot * 0.64, 20);

  const bars: Bar[] = buckets.map((bucket, index) => {
    const value = opts.value(bucket);
    const empty = value === 0;
    const partial = index === buckets.length - 1;
    const height = empty ? 2 : Math.max(2, ((BASE - TOP) * value) / top);
    return {
      key: bucket.bucket,
      x: LEFT + index * slot + (slot - width) / 2,
      y: BASE - height,
      width,
      height,
      empty,
      partial,
      title: `${bucketLabel(bucket.bucket, window)} — ${opts.words(value, bucket)}${
        partial ? " so far" : ""
      }`,
    };
  });

  return { bars, grid: grid(top, opts.axis), ticks: ticks(buckets, slot, width, window), top };
}

/** Spend per bucket. `02_costs.md` §3.2: the cost trend is the same
 *  single-hue bar treatment as runs, on an axis of its own. */
export function costGeometry(buckets: MetricsBucket[], window: string) {
  return barGeometry(buckets, window, {
    value: (bucket) => bucket.cost_usd,
    words: (value) => (value === 0 ? "nothing spent" : usd(value)),
    axis: (value) => usd(value),
  });
}

/** p50 and p95 latency: two steps of one hue, one axis, lines broken at every
 *  bucket that measured nothing. */
export function lineGeometry(buckets: MetricsBucket[], window: string) {
  const values = buckets.flatMap((b) => [b.latency_s.p50, b.latency_s.p95]);
  const measured = values.filter((v): v is number => v !== null && v !== undefined);
  const top = niceMax(Math.max(0, ...measured));
  const slot = buckets.length ? (RIGHT - LEFT) / buckets.length : 0;
  const width = Math.min(slot * 0.64, 20);

  const series: Series[] = (["p50", "p95"] as const).map((key) => {
    const points: (Point | null)[] = buckets.map((bucket, index) => {
      const value = bucket.latency_s[key];
      if (value === null || value === undefined) return null;
      const partial = index === buckets.length - 1;
      return {
        x: LEFT + index * slot + slot / 2,
        y: BASE - ((BASE - TOP) * value) / top,
        key: bucket.bucket,
        title: `${bucketLabel(bucket.bucket, window)} — ${key} ${seconds(value)}${
          partial ? " so far" : ""
        }`,
      };
    });
    const drawn = points.filter((p): p is Point => p !== null);
    return {
      key,
      label: key,
      segments: segmentsOf(points),
      points: drawn,
      label_at: drawn[drawn.length - 1] ?? null,
    };
  });

  return { series, grid: grid(top, (v) => String(v)), ticks: ticks(buckets, slot, width, window), top };
}

/**
 * One `d` per unbroken stretch of measured points.
 *
 * This is trap 1 made mechanical: a `null` ends the current path and the next
 * measured point starts a new one. A single measured point between two gaps is
 * a one-point segment, which draws nothing -- its dot carries it, which is why
 * every point is also rendered as a circle.
 */
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

function grid(top: number, label: (value: number) => string): Gridline[] {
  return [0, top / 2, top].map((value) => ({
    y: BASE - ((BASE - TOP) * value) / top,
    label: label(value),
  }));
}

function ticks(buckets: MetricsBucket[], slot: number, width: number, window: string): Tick[] {
  const last = buckets[buckets.length - 1];
  if (last === undefined) return [];
  const step = Math.max(1, Math.ceil(buckets.length / MAX_TICKS));
  const out: Tick[] = [];
  for (let index = 0; index < buckets.length; index += step) {
    const bucket = buckets[index];
    if (bucket === undefined) continue;
    out.push({
      x: LEFT + index * slot + slot / 2,
      label: bucketLabel(bucket.bucket, window, { short: true }),
    });
  }
  // The last bucket is the current one, and a reader looks for where "now" is.
  // Added only when the ladder above did not land near it, or the two labels
  // collide.
  const lastX = LEFT + (buckets.length - 1) * slot + slot / 2;
  const previous = out[out.length - 1];
  if (previous !== undefined && lastX - previous.x > slot + width) {
    out.push({ x: lastX, label: bucketLabel(last.bucket, window, { short: true }) });
  }
  return out;
}

/** The 1-2-5 ladder, extended so that 104 s tops out at 120 and not 200 --
 *  which is the axis the design draws, and the one that keeps a p95 near its
 *  120 s target legible against it.
 *
 *  `integerHalves` is for a count: a runs axis whose middle gridline reads
 *  `1.5` is labelling something that cannot happen. */
export function niceMax(value: number, opts: { integerHalves?: boolean } = {}): number {
  const steps = opts.integerHalves ? [2, 4, 6, 8, 10] : [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
  if (!(value > 0)) return steps[0] ?? 1;
  const decade = Math.pow(10, Math.floor(Math.log10(value)));
  // The decade itself, so 10 runs get a 0-5-10 axis rather than 0-10-20. Only
  // when its half is still whole, which is what `integerHalves` is asking for.
  if (opts.integerHalves && decade >= 2 && decade >= value - 1e-9) return decade;
  for (const step of steps) {
    const candidate = (step ?? 1) * decade;
    // Floating point: 1.2 * 100 is 120.00000000000001 on some values.
    if (candidate >= value - 1e-9) return Number(candidate.toPrecision(12));
  }
  return (steps[steps.length - 1] ?? 10) * decade * 10;
}
