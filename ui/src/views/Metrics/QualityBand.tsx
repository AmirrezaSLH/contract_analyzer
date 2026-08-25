import type { MetricsSummary } from "../../api/client";
import { Card } from "../../components/Card";
import { Label } from "../../components/Label";
import { StateChip } from "../../components/StateChip";
import { DASH, percent, ratio } from "./format";
import { meterFill, meterTick, statusOf, THRESHOLDS, type Threshold, type Tone } from "./thresholds";
import styles from "./MetricsView.module.css";

/**
 * Band 2 -- whether the answers can be trusted. Three meters.
 *
 * A meter is a label, a big value, a bar with a **black tick at the
 * threshold**, a status chip carrying words, and one line saying what a move
 * means. The three were chosen because they are the ones that decide whether
 * the output can be trusted at all: quotes found verbatim, results the
 * evaluator passed, results flagged for a human.
 *
 * **Rates carry their denominators, and `null` is not zero.** A rate is `null`
 * when its denominator is zero, so `quote_verification_rate: null` with
 * `quotes_total: 0` means *no quotes were produced* -- a different and more
 * alarming fact than 0% verified. Every meter renders the dash and the
 * denominator rather than a confident `0%`.
 */
export function QualityBand({ summary }: { summary: MetricsSummary | undefined }) {
  return (
    <section className={styles.band}>
      <div className={styles.bandHead}>
        <span className={styles.bandTitle}>Answer quality</span>
        <span className={styles.bandNote}>
          the three that decide whether the output can be trusted · the black tick is the threshold
        </span>
      </div>
      <div className={styles.meters}>
        {summary ? <Meters summary={summary} /> : <SkeletonMeters />}
      </div>
    </section>
  );
}

function Meters({ summary }: { summary: MetricsSummary }) {
  const { quality, runs } = summary;
  const evaluator = quality.evaluator;

  return (
    <>
      <Meter
        label="Quote verification"
        value={quality.quote_verification_rate}
        threshold={THRESHOLDS.quoteVerification}
        denominator={ratio(quality.quotes_verified, quality.quotes_total, "quotes")}
        note={THRESHOLDS.quoteVerification.action}
      />

      {/*
        The evaluator slot is honestly empty and must stay that way.
        `available` is false until the evaluator lands, so the meter is
        labelled for what it is actually showing, renders `value`, prints the
        payload's own `note`, and draws **no threshold tick** -- the 85% bound
        belongs to an accept rate and this is a cap rate. Labelling a cap rate
        as an accept rate is the one thing this slot exists to prevent.

        When the evaluator lands, `available` flips and `accept_rate` fills.
        The switch below is the only change this component needs.
      */}
      {evaluator.available ? (
        <Meter
          label="Evaluator accept"
          value={evaluator.accept_rate}
          threshold={THRESHOLDS.evaluatorAccept}
          denominator={ratio(runs.done, runs.settled, "settled runs")}
          note={THRESHOLDS.evaluatorAccept.action}
        />
      ) : (
        <Meter
          label="Cap rate (standing in for evaluator accept)"
          value={evaluator.value}
          threshold={null}
          denominator={ratio(quality.capped, runs.criteria, "criteria")}
          note={evaluator.note}
          status={{ tone: "neutral", words: "no evaluator yet" }}
        />
      )}

      <Meter
        label="Needs review"
        value={quality.needs_review_rate}
        threshold={THRESHOLDS.needsReview}
        denominator={ratio(quality.needs_review, runs.criteria, "criteria")}
        note={THRESHOLDS.needsReview.action}
      />
    </>
  );
}

interface MeterProps {
  label: string;
  value: number | null | undefined;
  /** `null` where there is no honest bound to draw -- the standing-in cap
   *  rate. The bar is then a proportion with no tick, which is the truth. */
  threshold: Threshold | null;
  denominator: string;
  note: string;
  status?: { tone: Tone; words: string };
}

function Meter({ label, value, threshold, denominator, note, status }: MeterProps) {
  const state = status ?? (threshold ? statusOf(value, threshold) : { tone: "neutral" as Tone, words: "" });
  // With no threshold the bar is drawn against its own full scale: a cap rate
  // of 2.3% is a sliver, and a sliver is what 2.3% is.
  const scale = threshold ?? { limit: 1, direction: "min" as const, label: "", action: "" };
  const tick = meterTick(threshold);

  return (
    <Card className={styles.meter}>
      <div className={styles.meterHead}>
        <Label>{label}</Label>
        {state.words ? <StateChip state={state.tone} label={state.words} size="sm" /> : null}
      </div>

      <div className={styles.meterValue}>
        <span className={styles.meterNumber}>{percent(value)}</span>
        <span className={styles.meterTarget}>
          {threshold ? `${threshold.label} · ` : ""}
          {denominator}
        </span>
      </div>

      <div
        className={styles.bar}
        role="img"
        aria-label={`${label}: ${value === null || value === undefined ? DASH : percent(value)}${
          threshold ? `, ${threshold.label}` : ""
        }`}
      >
        <div
          className={`${styles.fill} ${FILL[state.tone] ?? ""}`}
          style={{ width: `${meterFill(value, scale) * 100}%` }}
        />
        {tick === null ? null : <div className={styles.tick} style={{ left: `${tick * 100}%` }} />}
      </div>

      <span className={styles.meterNote}>{note}</span>
    </Card>
  );
}

const FILL: Record<Tone, string | undefined> = {
  good: styles.fillGood,
  warn: styles.fillWarn,
  neutral: styles.fillNeutral,
};

function SkeletonMeters() {
  return (
    <>
      {[0, 1, 2].map((index) => (
        <Card key={index} className={styles.meter}>
          <span className={styles.skeletonSub} aria-hidden />
          <span className={styles.skeleton} aria-hidden />
          <div className={styles.bar} />
          <span className={styles.skeletonSub} aria-hidden />
        </Card>
      ))}
    </>
  );
}
