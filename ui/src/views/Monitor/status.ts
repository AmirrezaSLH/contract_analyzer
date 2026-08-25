import type { ChipState } from "../../components/StateChip";
import { percent } from "../Metrics/format";

/**
 * Chips for this page. Colour never travels without words.
 *
 * Host used-% is the scale the plan named: under 20% plenty, over 90% tight,
 * the band in between filling. Reliability rates only light when they miss.
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

/** Exhausted-retry rate. > 1% of upstream calls. */
export function exhaustedChip(rate: number | null | undefined): Chip | null {
  if (rate === null || rate === undefined) return null;
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

/** Tile title: HTTP statuses vs ConnectError / timeout class names. */
export function topReasonTitle(reason: string | null | undefined): string {
  if (!reason) return "Top retry reason";
  if (reason.startsWith("HTTP ")) return "Top HTTP status";
  return "Top error type";
}

/** `HTTP 429` plus that reason's share of retry/exhausted events. */
export function topReasonLabel(
  reason: string | null | undefined,
  share: number | null | undefined,
): string {
  if (!reason) return "Null";
  const code = reason.startsWith("HTTP ") ? reason.slice(5) : reason;
  if (share === null || share === undefined) return code;
  return `${code} · ${percent(share)}`;
}
