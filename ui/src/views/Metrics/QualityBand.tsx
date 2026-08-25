import type { MetricsSummary } from "../../api/client";
import { Card } from "../../components/Card";
import { Label } from "../../components/Label";
import { StateChip } from "../../components/StateChip";
import { DASH, percent, ratio } from "./format";
import { meterFill, meterTick, statusOf, THRESHOLDS, type Threshold, type Tone } from "./thresholds";
import styles from "./MetricsView.module.css";

/**
 * Band 2 -- whether the answers can be trusted. Two meters.
 *
 * A meter is a label, a big value, a bar with a **black tick at the
 * threshold**, a status chip carrying words, and one line saying what a move
 * means. The two that decide whether the output can be trusted at all:
 * quotes found verbatim, and results flagged for a human.
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
      </div>
      <div className={styles.meters}>
        {summary ? <Meters summary={summary} /> : <SkeletonMeters />}
      </div>
    </section>
  );
}

function Meters({ summary }: { summary: MetricsSummary }) {
  const { quality, runs } = summary;

  return (
    <>
      <Meter
        label="Quote verification"
        value={quality.quote_verification_rate}
        threshold={THRESHOLDS.quoteVerification}
        denominator={ratio(quality.quotes_verified, quality.quotes_total, "quotes")}
        note={THRESHOLDS.quoteVerification.action}
      />

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
  threshold: Threshold;
  denominator: string;
  note: string;
}

function Meter({ label, value, threshold, denominator, note }: MeterProps) {
  const state = statusOf(value, threshold);
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
          {threshold.label} · {denominator}
        </span>
      </div>

      <div
        className={styles.bar}
        role="img"
        aria-label={`${label}: ${value === null || value === undefined ? DASH : percent(value)}, ${threshold.label}`}
      >
        <div
          className={`${styles.fill} ${FILL[state.tone] ?? ""}`}
          style={{ width: `${meterFill(value, threshold) * 100}%` }}
        />
        <div className={styles.tick} style={{ left: `${tick * 100}%` }} />
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
      {[0, 1].map((index) => (
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
