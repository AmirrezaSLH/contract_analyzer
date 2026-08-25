import type { MetricsSummary } from "../../api/client";
import { MetricTile } from "../../components/MetricTile";
import { StateChip } from "../../components/StateChip";
import { percent, plural, ratio, seconds, usd } from "./format";
import { costTailRatio, statusOf, THRESHOLDS } from "./thresholds";
import styles from "./MetricsView.module.css";

interface Props {
  summary: MetricsSummary | undefined;
}

/**
 * Band 1 -- what is happening now. Four tiles.
 *
 * **Every threshold travels in its own sub-line**, so the number and the bar
 * it has to clear are never separated, and a tile that crosses one takes a
 * chip that carries the words. Colour says nothing on its own here.
 */
export function NowBand({ summary }: Props) {
  if (!summary) return <SkeletonTiles />;

  const { runs, reliability, latency_s, cost_usd } = summary;
  const failure = statusOf(reliability.failure_rate, THRESHOLDS.failureRate);
  const latency = statusOf(latency_s.p95, THRESHOLDS.latencyP95);
  // Two things can be wrong with spend, and they are different alerts. The
  // window total against the budget is the one that pauses new runs; the p95
  // against the p50 is the "one run went wild" tripwire, which fires long
  // before the budget does and is the one an average would hide. The budget
  // wins the chip when both are lit -- it is the one with an action behind it.
  const spend = statusOf(cost_usd.total, THRESHOLDS.dailyBudget);
  const tail = statusOf(costTailRatio(cost_usd.p50, cost_usd.p95), THRESHOLDS.costTail);
  const spendChip = spend.tone === "warn" ? spend : tail.tone === "warn" ? { ...tail, words: "tail" } : spend;

  return (
    <div className={styles.tiles}>
      <MetricTile
        label="Runs"
        value={String(runs.total)}
        sub={`${plural(summary.documents, "document")} · ${plural(runs.criteria, "criterion", "criteria")}`}
      />
      <MetricTile
        label="Failure rate"
        value={percent(reliability.failure_rate)}
        chip={<Chip tone={failure.tone} words={failure.words} />}
        sub={`${THRESHOLDS.failureRate.label} · ${ratio(
          reliability.failed + reliability.interrupted,
          runs.settled,
        )} settled`}
      />
      <MetricTile
        label="p95 latency"
        value={seconds(latency_s.p95)}
        chip={<Chip tone={latency.tone} words={latency.words} />}
        sub={`${THRESHOLDS.latencyP95.label} · p50 ${seconds(latency_s.p50)}`}
      />
      {/*
        Dollars on the tile, tokens and calls behind it: cost is the only
        family where three numbers describe the same event, and promoting all
        three says the same thing thrice. The sub-line is **p50 per run**, not
        the mean -- `02_costs.md` §1 -- because the mean is the number that
        hides the run that went wild, and the tail is the whole reason this
        tile has a tripwire.
      */}
      <MetricTile
        label="Spend"
        value={usd(cost_usd.total)}
        chip={<Chip tone={spendChip.tone} words={spendChip.words} />}
        sub={`${usd(cost_usd.p50)} per run (p50) · ${THRESHOLDS.dailyBudget.label}`}
      />
    </div>
  );
}

/** A threshold chip. Rendered only when there is something to say: a tile that
 *  is healthy and unremarkable should not carry five words of reassurance. */
function Chip({ tone, words }: { tone: "good" | "warn" | "neutral"; words: string }) {
  if (tone === "good") return null;
  return <StateChip state={tone} label={words} size="sm" />;
}

function SkeletonTiles() {
  return (
    <div className={styles.tiles}>
      {[0, 1, 2, 3].map((index) => (
        <MetricTile
          key={index}
          label=""
          value={<span className={styles.skeleton} aria-hidden />}
          sub={<span className={styles.skeletonSub} aria-hidden />}
        />
      ))}
    </div>
  );
}
