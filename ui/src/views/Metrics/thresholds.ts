/**
 * The thresholds, and what a number does when it crosses one.
 *
 * **Every value here is from `KPI_01/00_README.md` § the initial set. Nothing
 * on this page invents a metric or a threshold**, and a number changed here
 * without changing that document is a dashboard that disagrees with its own
 * design record.
 *
 * Two are measured and two are judgement calls, which the design says out
 * loud: cost is grounded (~$0.96 per run, `02_costs.md`) and job duration is
 * anchored (~60 s parallel measured); the 99% quote-verification and 10%
 * needs-review targets are defensible but unmeasured.
 *
 * A status is a **word**, always. Colour never carries a fact on this page:
 * the chip contains its own meaning, so the tile survives greyscale, colour
 * blindness and print. That is the same rule `StateChip` is built on.
 */

export type Tone = "good" | "warn" | "neutral";

export interface Status {
  tone: Tone;
  /** The chip's text. Never omitted -- a chip with its words removed is a bug,
   *  not a compact variant. */
  words: string;
}

export interface Threshold {
  /** The bound itself, on the metric's own scale: a rate as a fraction, a
   *  job duration in seconds, a spend in dollars. */
  limit: number;
  /** `max`: pass at or below the limit. `min`: pass at or above it. */
  direction: "max" | "min";
  /** The sub-line that travels with the number, so the value and the bar it
   *  must clear are never separated. */
  label: string;
  /** What firing means, in one line. The meters print it; a threshold with no
   *  action behind it is a number on a screen. */
  action: string;
}

export const THRESHOLDS = {
  /** Quotes found verbatim in the passage they cite -- the hallucination
   *  check, and the strongest quality signal in the system. */
  quoteVerification: {
    limit: 0.99,
    direction: "min",
    label: "target ≥ 99%",
    action:
      "Quotes found verbatim in the passage they cite. This is the hallucination check; below 99% something is being invented.",
  },
  /** Results flagged for a human. */
  needsReview: {
    limit: 0.1,
    direction: "max",
    label: "target ≤ 10%",
    action:
      "Results flagged for a human. Rising means the contracts are getting harder, or retrieval is getting worse.",
  },
  /** `failed + interrupted` over `settled`. Never done-but-needs-review. */
  failureRate: {
    limit: 0.02,
    direction: "max",
    label: "target ≤ 2%",
    action: "Runs that failed or were interrupted. Needs-review is quality and is not in this.",
  },
  /** p95, never the mean. ~60 s parallel measured, with headroom on top. */
  jobDurationP95: {
    limit: 120,
    direction: "max",
    label: "target ≤ 120 s",
    action: "The tail is what breaks a demo, which is why this is p95 and not the mean.",
  },
  /** The window's spend against the day's budget. A breach pauses new runs. */
  dailyBudget: {
    limit: 50,
    direction: "max",
    label: "budget $50/day",
    action:
      "A budget breach pauses new runs and starts the re-target conversation, rather than a silent overrun.",
  },
  /** The "one run went wild" tripwire, `02_costs.md` §3: p95 cost per run no
   *  more than about twice p50. It is a **ratio**, not a dollar figure, which
   *  is the point -- the tail is what a budget dies of, and a fixed dollar cap
   *  would have to be re-set every time the corpus changed. The mean carries
   *  no threshold at all; it is context. */
  costTail: {
    limit: 2,
    direction: "max",
    label: "p95 ≤ 2× p50",
    action: "One run costing twice the median is a run that went wild — open it, do not average it away.",
  },
} as const satisfies Record<string, Threshold>;

/**
 * The cost tail, as a ratio: p95 over p50.
 *
 * `null` when either percentile is missing, and when p50 is zero -- a division
 * that would otherwise report `Infinity` as a threshold breach on a window
 * where nothing was spent. At n=1 the two percentiles are the same value and
 * this is exactly 1, which is correct and not a suspiciously healthy number.
 */
export function costTailRatio(
  p50: number | null | undefined,
  p95: number | null | undefined,
): number | null {
  if (p50 === null || p50 === undefined || p95 === null || p95 === undefined) return null;
  if (p50 <= 0) return null;
  return p95 / p50;
}

/**
 * Where a value sits against its bound.
 *
 * `null` is `neutral`, not a pass: a rate with no denominator has not been
 * measured, and a green chip over an unmeasured number is a lie.
 */
export function statusOf(value: number | null | undefined, threshold: Threshold): Status {
  if (value === null || value === undefined) return { tone: "neutral", words: "not measured" };
  const ok = threshold.direction === "max" ? value <= threshold.limit : value >= threshold.limit;
  if (ok) return { tone: "good", words: "healthy" };
  return { tone: "warn", words: threshold.direction === "max" ? "over target" : "below target" };
}

/**
 * How far along a meter's bar the fill goes, as a fraction of its width.
 *
 * A `max` meter -- needs-review at 6.1% against a 10% ceiling -- would be a
 * sliver if it were drawn on its own 0-1 scale, and the tick at 10% would be
 * invisible at the left edge. Both are drawn against a **track top**: the
 * limit with headroom, so the tick lands about two thirds along and a value
 * that breaches it still has bar left to show how far.
 */
export function meterScale(threshold: Threshold): number {
  return threshold.direction === "min" ? 1 : threshold.limit * 1.5;
}

/** A fraction of the bar, clamped to it. A value past the top pins at full
 *  rather than overflowing the track. */
export function meterFill(value: number | null | undefined, threshold: Threshold): number {
  if (value === null || value === undefined) return 0;
  return clamp(value / meterScale(threshold));
}

/** Where the black tick goes, as a fraction of the bar. */
export function meterTick(threshold: Threshold): number {
  return clamp(threshold.limit / meterScale(threshold));
}

function clamp(fraction: number): number {
  return Math.max(0, Math.min(1, fraction));
}
