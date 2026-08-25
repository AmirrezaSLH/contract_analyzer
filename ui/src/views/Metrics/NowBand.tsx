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
 * Band 1 -- what is happening now. Five tiles.
 *
 * **Every threshold travels in its own sub-line**, so the number and the bar
 * it has to clear are never separated, and a tile that crosses one takes a
 * chip that carries the words. Colour says nothing on its own here.
 *
 * Spend is two tiles, not one: the window total against the budget, and the
 * p95 per job against the tail tripwire. Combining them hid which number was
 * which, and which alert belonged to which.
 */
export function NowBand({ summary }: Props) {
  if (!summary) return <SkeletonTiles />;

  const { runs, reliability, job_duration_s, cost_usd } = summary;
  const failure = statusOf(reliability.failure_rate, THRESHOLDS.failureRate);
  const duration = statusOf(job_duration_s.p95, THRESHOLDS.jobDurationP95);
  const spend = statusOf(cost_usd.total, THRESHOLDS.dailyBudget);
  const tail = statusOf(costTailRatio(cost_usd.p50, cost_usd.p95), THRESHOLDS.costTail);

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
        label="p95 job duration"
        value={seconds(job_duration_s.p95)}
        chip={<Chip tone={duration.tone} words={duration.words} />}
        sub={`${THRESHOLDS.jobDurationP95.label} · p50 ${seconds(job_duration_s.p50)}`}
      />
      <MetricTile
        label="Total spend"
        value={usd(cost_usd.total)}
        chip={<Chip tone={spend.tone} words={spend.words} />}
        sub={`${summary.window} window · ${THRESHOLDS.dailyBudget.label}`}
      />
      <MetricTile
        label="p95 job cost"
        value={usd(cost_usd.p95)}
        chip={<Chip tone={tail.tone} words={tail.words} />}
        sub={`${THRESHOLDS.costTail.label} · p50 ${usd(cost_usd.p50)}`}
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
      {[0, 1, 2, 3, 4].map((index) => (
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
