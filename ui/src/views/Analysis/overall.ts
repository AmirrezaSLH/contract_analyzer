import type { ComplianceResult, ResolvedQuote } from "../../api/client";
import type { ChipState } from "../../components/StateChip";

/**
 * The three presentational computations the analysis view is allowed to make.
 *
 * The rule is that this UI contains no logic the API does not have: it parses
 * no PDF, opens no database, calls no model and knows no prompt. These three
 * are the documented exceptions, and they are exceptions because they are
 * *presentation* decisions rather than facts about a contract. If `totals`
 * ever grows them, prefer those and delete this file.
 */

/** Worst-of-five. Severity ordering is a presentation decision -- a report
 *  with one Non-Compliant criterion is not "mostly compliant". */
export function overallState(results: ComplianceResult[]): ChipState {
  if (results.length === 0) return "neutral";
  if (results.some((r) => r.compliance_state === "Non-Compliant")) return "Non-Compliant";
  if (results.some((r) => r.compliance_state === "Partially Compliant")) return "Partially Compliant";
  return "Fully Compliant";
}

/** Two counts over data already on screen. */
export function quoteCounts(results: ComplianceResult[]): { verified: number; total: number } {
  const quotes = results.flatMap((r) => (r.relevant_quotes ?? []) as ResolvedQuote[]);
  return { verified: quotes.filter((q) => q.verified).length, total: quotes.length };
}

export function needsReviewCount(results: ComplianceResult[]): number {
  return results.filter((r) => r.needs_review).length;
}

/** `k of n met`, for the collapsed row. */
export function subSummary(result: ComplianceResult): string {
  const subs = result.sub_requirements ?? [];
  const met = subs.filter((s) => s.status === "met").length;
  return `${met} of ${subs.length} met`;
}
