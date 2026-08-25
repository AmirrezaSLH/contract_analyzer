import type { ChipState } from "../../components/StateChip";

/**
 * Chips for this page. Colour never travels without words.
 *
 * Host used-% is the scale the plan named: under 20% plenty, over 90% tight,
 * the band in between filling. HTTP 5xx and the two reliability rates only
 * light when they miss — a wall of green "answering" chips is not a status.
 */

export interface Chip {
  state: ChipState;
  words: string;
}

export function usedChip(fraction: number | null | undefined): Chip | null {
  if (fraction === null || fraction === undefined) return null;
  if (fraction < 0.2) return { state: "good", words: "plenty" };
  if (fraction > 0.9) return { state: "Non-Compliant", words: "tight" };
  return { state: "warn", words: "filling" };
}

/** 5xx rate over the last five minutes. > 1% is failing. */
export function fiveXxChip(rate: number): Chip | null {
  if (rate > 0.01) return { state: "Non-Compliant", words: "failing" };
  return null;
}

/** Exhausted-retry rate. > 1% of upstream calls. */
export function exhaustedChip(rate: number): Chip | null {
  if (rate > 0.01) return { state: "Non-Compliant", words: "failing" };
  return null;
}

/** Stage error rate, only with enough samples. 1-of-1 is not 100%.
 *  No data is not a chip — the tile says null. */
export function stageChip(rate: number | null | undefined, n: number): Chip | null {
  if (n === 0) return null;
  if (n < 10) return { state: "neutral", words: "not enough" };
  if (rate === null || rate === undefined) return null;
  if (rate > 0.05) return { state: "Non-Compliant", words: "failing" };
  return null;
}
