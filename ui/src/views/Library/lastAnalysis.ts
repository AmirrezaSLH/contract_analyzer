import type { LastAnalysisOut } from "../../api/client";
import type { ChipState } from "../../components/StateChip";

export interface LastAnalysisWords {
  /** The library chip. */
  label: string;
  /** The sidebar's one-line version. */
  short: string;
  state: ChipState;
}

/**
 * The words for a document's last analysis.
 *
 * One of the three documented exceptions to the no-logic rule, and the reason
 * `last_analysis.states` is a **count per state** rather than a summary
 * sentence: the API returns counts precisely so that each client picks its own
 * words, and the next consumer will want different ones.
 */
export function lastAnalysisWords(last: LastAnalysisOut | null | undefined): LastAnalysisWords {
  if (!last) return { label: "Not analysed", short: "not analysed", state: "neutral" };

  if (last.status !== "done" && last.status !== "cancelled") {
    // A run in flight, or one a dead process left behind. The status is the
    // honest word for all of them, and `interrupted` is not `failed`.
    return { label: WORDS[last.status] ?? last.status, short: last.status, state: "neutral" };
  }

  const states = last.states ?? {};
  const full = states["Fully Compliant"] ?? 0;
  const partial = states["Partially Compliant"] ?? 0;
  const non = states["Non-Compliant"] ?? 0;
  const total = full + partial + non;
  if (total === 0) return { label: "No verdicts", short: "no verdicts", state: "neutral" };

  const gaps = partial + non;
  if (gaps === 0) {
    return {
      label: `${full} of ${total} compliant`,
      short: "analysed",
      state: "Fully Compliant",
    };
  }
  return {
    label: `${gaps} gap${gaps === 1 ? "" : "s"} found`,
    short: `${gaps} gap${gaps === 1 ? "" : "s"}`,
    // The worst state present, so a single Non-Compliant is not softened into
    // amber by four partials beside it.
    state: non > 0 ? "Non-Compliant" : "Partially Compliant",
  };
}

const WORDS: Record<string, string | undefined> = {
  queued: "Queued",
  running: "Analysing",
  failed: "Analysis failed",
  interrupted: "Interrupted",
};
