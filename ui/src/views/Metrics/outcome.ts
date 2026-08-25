import type { RunRow } from "../../api/client";
import type { Status } from "./thresholds";

/**
 * What happened to one run, in words.
 *
 * **Three outcomes, not two.** `failed` and `interrupted` are reliability;
 * `needs_review` is quality. Folding them together under-counts exactly what a
 * reviewer should care about, which is why the failure-rate tile and this
 * column disagree on purpose: a run can be a clean success by the tile and
 * still say `2 need review` here.
 *
 * `interrupted` is not a synonym for `failed` either. It is what `reconcile`
 * writes over a row the process died holding -- the model refusing and the
 * machine going away are different events, and the second one is worth
 * re-running.
 *
 * One deviation from `02_kpi_page.md` §3.4, and it is deliberate. The spec
 * writes the clean case as `5 of 5 compliant`, but the three fields it says to
 * compose from -- `status`, `needs_review`, `criteria_completed` -- do not
 * carry a compliance state: `criteria_completed` counts criteria that
 * *finished*, whatever verdict they reached. A run of five Non-Compliant
 * criteria would read `5 of 5 compliant`. So the words are **`5 of 5
 * complete`**, and the verdict stays where it is measured, on the report.
 */
export function runOutcome(run: RunRow): Status {
  const flagged = run.needs_review ?? 0;
  const done = run.criteria_completed ?? 0;
  const asked = run.criteria_requested ?? 0;

  switch (run.status) {
    case "failed":
      return { tone: "warn", words: "failed" };
    case "interrupted":
      return { tone: "warn", words: "interrupted" };
    case "cancelled":
      return { tone: "neutral", words: "cancelled" };
    case "running":
      return { tone: "neutral", words: asked ? `${done} of ${asked} running` : "running" };
    case "queued":
      return { tone: "neutral", words: "queued" };
    case "done":
      if (flagged > 0) {
        return { tone: "warn", words: flagged === 1 ? "1 needs review" : `${flagged} need review` };
      }
      return { tone: "good", words: `${done} of ${asked || done} complete` };
    default:
      // A status this UI has not been taught. Rendered rather than swallowed:
      // an unknown word is better than a blank cell that says nothing happened.
      return { tone: "neutral", words: run.status };
  }
}
