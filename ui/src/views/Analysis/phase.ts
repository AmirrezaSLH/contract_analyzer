/**
 * Which of the analysis view's states is on screen.
 *
 * Extracted from the view and made pure because getting it wrong is invisible:
 * the failure mode is *nothing renders*, which looks like a dead button rather
 * than like an error. That is exactly what happened -- see the note below.
 */
export type Phase =
  /** Still finding out. Render nothing rather than flashing an empty state. */
  | "loading"
  /** No analysis has ever been run on this document. The Run button lives here. */
  | "none"
  /** Queued or running: the progress card. */
  | "live"
  /** Terminal: the report, or the banner explaining why there is not one. */
  | "settled";

export interface PhaseInput {
  /** Whether `GET /documents/{id}` has answered yet. Until it has, we do not
   *  know whether this document has a `last_analysis`. */
  documentLoading: boolean;
  /** The id derived from `last_analysis` or from a submission, or null. */
  analysisId: string | null;
  /** Whether the analysis query is fetching. **Not `isPending`.** */
  analysisFetching: boolean;
  /** The status the analysis query has returned, if any. */
  status: string | undefined;
  /** A submission is in flight. */
  submitting: boolean;
}

export function analysisPhase(input: PhaseInput): Phase {
  const { documentLoading, analysisId, analysisFetching, status, submitting } = input;

  if (documentLoading) return "loading";
  if (submitting) return "loading";

  // **The bug this function exists to prevent.** TanStack Query v5 reports
  // `isPending: true` for a *disabled* query -- one whose `enabled` is false
  // because there is nothing to fetch. A guard written as `!query.isPending`
  // is therefore never true on a document that has never been analysed, and
  // the empty state carrying the Run button never renders: the page shows a
  // header, a tab bar, and nothing else. A disabled query is not loading; it
  // is idle. `analysisId === null` is the honest test, and `isFetching` is the
  // honest field.
  if (analysisId === null) return "none";

  if (status === undefined) return analysisFetching ? "loading" : "none";
  if (status === "queued" || status === "running") return "live";
  return "settled";
}
