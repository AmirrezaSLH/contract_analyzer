import { useState } from "react";

import type { ComplianceResult, CriterionOut, ResolvedQuote } from "../../api/client";
import { Button } from "../../components/Button";
import { Disclosure } from "../../components/Disclosure";
import { Label } from "../../components/Label";
import { QuoteCard } from "../../components/QuoteCard";
import { StateChip } from "../../components/StateChip";
import { SubMarker } from "../../components/SubMarker";
import { subSummary } from "./overall";
import styles from "./AnalysisView.module.css";

/** How many quotes a collapsed body shows before the disclosure. */
const SHOWN = 2;

interface Props {
  index: number;
  result: ComplianceResult;
  /** `GET /criteria`, for the *full requirement text* of each sub-requirement.
   *  The report carries the text too; this is the fallback when a stored
   *  report predates a wording change. */
  criterion: CriterionOut | undefined;
  open: boolean;
  onToggle: () => void;
}

export function CriterionRow({ index, result, criterion, open, onToggle }: Props) {
  const [all, setAll] = useState(false);
  const quotes = (result.relevant_quotes ?? []) as ResolvedQuote[];
  const shown = all ? quotes : quotes.slice(0, SHOWN);
  const verified = quotes.filter((q) => q.verified).length;

  return (
    <Disclosure
      open={open}
      onToggle={onToggle}
      header={
        // All four data points, visible while collapsed. This is where a
        // reviewer decides what to open; demoting any of it to the inside of
        // the row is a functional regression, not a cosmetic one.
        <>
          <span className={styles.critTitle}>
            {index} · {result.compliance_requirement}
          </span>
          {result.needs_review ? <span className={styles.review}>needs review</span> : null}
          <span className={styles.critSubs}>{subSummary(result)}</span>
          <span className={styles.critConf}>conf {result.confidence.toFixed(2)}</span>
          <StateChip state={result.compliance_state} />
        </>
      }
    >
      {result.needs_review ? (
        <p className={styles.reviewNote}>
          Flagged for review
          {result.unresolved_errors && result.unresolved_errors.length > 0
            ? `: ${result.unresolved_errors.join("; ")}`
            : ". The confidence was capped because a check did not pass cleanly."}
        </p>
      ) : null}

      <section className={styles.block}>
        <Label>Sub-requirements</Label>
        <div className={styles.subs}>
          {(result.sub_requirements ?? []).map((sub) => (
            <div key={sub.id} className={styles.sub}>
              <SubMarker status={sub.status} />
              {/* The requirement text, not the id: `GOV-04` does not say what
                  was checked. */}
              <span className={styles.subText}>{requirementText(sub, criterion)}</span>
            </div>
          ))}
        </div>
      </section>

      {quotes.length > 0 ? (
        <section className={styles.block}>
          <div className={styles.quotesHead}>
            <Label>Relevant quotes</Label>
            <span className={styles.quotesCount}>
              showing {shown.length} of {quotes.length} —{" "}
              {verified === quotes.length
                ? "all verified verbatim"
                : `${quotes.length - verified} not found verbatim`}
            </span>
          </div>
          <div className={styles.quotes}>
            {shown.map((quote) => (
              <QuoteCard key={`${quote.evidence_id}-${quote.text.slice(0, 24)}`} quote={quote} />
            ))}
          </div>
          {quotes.length > SHOWN ? (
            <Button variant="tertiary" onClick={() => setAll((value) => !value)}>
              {all ? "Show fewer quotes" : `Show all ${quotes.length} quotes`}
            </Button>
          ) : null}
        </section>
      ) : null}

      <section className={styles.block}>
        <Label>Rationale</Label>
        <p className={styles.rationale}>{result.rationale}</p>
      </section>

      <footer className={styles.critFoot}>
        <span>{result.latency_s ? `${result.latency_s.toFixed(1)} s` : "—"}</span>
        <span>${result.cost_usd.toFixed(3)}</span>
        <span>
          {result.tool_calls} tool call{result.tool_calls === 1 ? "" : "s"}
        </span>
        <span>{result.ended_by === "cap" ? "stopped by a counter" : "finished by the model"}</span>
      </footer>
    </Disclosure>
  );
}

function requirementText(
  sub: { id: string; requirement: string },
  criterion: CriterionOut | undefined,
): string {
  return (
    sub.requirement ||
    criterion?.sub_requirements?.find((s) => s.id === sub.id)?.requirement ||
    sub.id
  );
}
