import type { ReactNode } from "react";
import type { MetricsBucket, MetricsWindow } from "../../api/client";
import { Card } from "../../components/Card";
import {
  costLineGeometry,
  LABEL_Y,
  lineGeometry,
  runsLineGeometry,
  VIEW,
  type Gridline,
  type Series,
  type Tick,
} from "./charts";
import { plural, seconds, usd } from "./format";
import { bucketWords } from "./WindowSelector";
import styles from "./MetricsView.module.css";

interface Props {
  buckets: MetricsBucket[] | undefined;
  window: MetricsWindow;
}

/**
 * Three line charts, one axis each: runs initiated per bucket, job-duration
 * p50/p95, job-cost p50/p95. Empty duration and spend buckets break the line;
 * a quiet hour is a zero on runs.
 */
export function TrendBand({ buckets, window }: Props) {
  return (
    <div className={styles.charts3}>
      <RunsChart buckets={buckets} window={window} />
      <JobDurationChart buckets={buckets} window={window} />
      <CostChart buckets={buckets} window={window} />
    </div>
  );
}

function RunsChart({ buckets, window }: Props) {
  const rows = buckets ?? [];
  const total = rows.reduce((sum, bucket) => sum + bucket.runs, 0);
  const { series, grid, ticks } = runsLineGeometry(rows, window);
  const line = series[0];

  return (
    <Card className={styles.chart}>
      <span className={styles.chartTitle}>Runs</span>
      <span className={styles.chartSub}>
        {plural(total, "run")} initiated · {bucketWords(window)}
      </span>
      {rows.length === 0 || !line ? (
        <p className={styles.chartEmpty}>No buckets in this window yet.</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
          width="100%"
          role="img"
          aria-label={`Runs initiated per bucket: ${plural(total, "run")} over ${window}`}
        >
          <Axes grid={grid} ticks={ticks} />
          <LineMarks entry={line} stroke={styles.line1 ?? ""} fill={styles.dot1 ?? ""} />
        </svg>
      )}
    </Card>
  );
}

function JobDurationChart({ buckets, window }: Props) {
  const rows = buckets ?? [];
  const { series, grid, ticks } = lineGeometry(rows, window);
  return (
    <DualLineChart
      title="Job duration"
      sub={`seconds · buckets with runs`}
      aria="Job duration in seconds, p50 and p95 per bucket"
      empty={
        <>
          No run settled in this window, so there is no percentile to draw. An empty bucket has no
          job duration — {seconds(null)} rather than zero.
        </>
      }
      series={series}
      grid={grid}
      ticks={ticks}
    />
  );
}

function CostChart({ buckets, window }: Props) {
  const rows = buckets ?? [];
  const { series, grid, ticks } = costLineGeometry(rows, window);
  return (
    <DualLineChart
      title="Spend"
      sub={`per job · buckets with runs`}
      aria="Job cost in dollars, p50 and p95 per bucket"
      empty={
        <>
          No run settled in this window, so there is no percentile to draw. An empty bucket has no
          job cost — {usd(null)} rather than zero.
        </>
      }
      series={series}
      grid={grid}
      ticks={ticks}
    />
  );
}

function DualLineChart({
  title,
  sub,
  aria,
  empty,
  series,
  grid,
  ticks,
}: {
  title: string;
  sub: string;
  aria: string;
  empty: ReactNode;
  series: Series[];
  grid: Gridline[];
  ticks: Tick[];
}) {
  const measured = series.some((entry) => entry.points.length > 0);

  return (
    <Card className={styles.chart}>
      <div className={styles.chartHead}>
        <span className={styles.chartTitle}>{title}</span>
        <div className={styles.legend}>
          <span className={styles.legendItem}>
            <span className={`${styles.swatch} ${styles.swatch2}`} />
            p50
          </span>
          <span className={styles.legendItem}>
            <span className={`${styles.swatch} ${styles.swatch1}`} />
            p95
          </span>
        </div>
      </div>
      <span className={styles.chartSub}>
        {sub}
        {measured ? "" : " · nothing measured yet"}
      </span>
      {measured ? (
        <svg viewBox={`0 0 ${VIEW.width} ${VIEW.height}`} width="100%" role="img" aria-label={aria}>
          <Axes grid={grid} ticks={ticks} />
          {series.map((entry) => (
            <LineMarks
              key={entry.key}
              entry={entry}
              stroke={entry.key === "p95" ? (styles.line1 ?? "") : (styles.line2 ?? "")}
              fill={entry.key === "p95" ? (styles.dot1 ?? "") : (styles.dot2 ?? "")}
              labelClass={entry.key === "p95" ? styles.seriesLabel1 : styles.seriesLabel2}
            />
          ))}
        </svg>
      ) : (
        <p className={styles.chartEmpty}>{empty}</p>
      )}
    </Card>
  );
}

function LineMarks({
  entry,
  stroke,
  fill,
  labelClass,
}: {
  entry: Series;
  stroke: string;
  fill: string;
  labelClass?: string;
}) {
  return (
    <g>
      {entry.segments.map((d) => (
        <path
          key={d}
          d={d}
          fill="none"
          strokeWidth={2}
          strokeLinecap="round"
          strokeLinejoin="round"
          className={stroke}
        />
      ))}
      {entry.points.map((point) => (
        <circle
          key={point.key}
          cx={point.x}
          cy={point.y}
          r={3.5}
          className={`${fill} ${point.partial ? styles.dotPartial : ""}`}
        >
          <title>{point.title}</title>
        </circle>
      ))}
      {labelClass && entry.label_at ? (
        <text
          x={Math.min(entry.label_at.x + 8, VIEW.width - 22)}
          y={entry.label_at.y + 3.5}
          className={labelClass}
        >
          {entry.label}
        </text>
      ) : null}
    </g>
  );
}

function Axes({
  grid,
  ticks,
}: {
  grid: Gridline[];
  ticks: Tick[];
}) {
  return (
    <>
      {grid.map((line) => (
        <g key={line.label}>
          <line x1={30} y1={line.y} x2={434} y2={line.y} className={styles.gridline} />
          <text x={22} y={line.y + 3.5} textAnchor="end" className={styles.axisLabel}>
            {line.label}
          </text>
        </g>
      ))}
      {ticks.map((tick) => (
        <text
          key={`${tick.x}-${tick.label}`}
          x={tick.x}
          y={LABEL_Y}
          textAnchor="middle"
          className={styles.axisLabel}
        >
          {tick.label}
        </text>
      ))}
    </>
  );
}
