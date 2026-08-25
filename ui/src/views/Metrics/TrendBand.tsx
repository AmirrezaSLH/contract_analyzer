import type { MetricsBucket, MetricsWindow } from "../../api/client";
import { Card } from "../../components/Card";
import { barGeometry, costGeometry, LABEL_Y, lineGeometry, VIEW } from "./charts";
import { plural, seconds, usd } from "./format";
import { bucketWords } from "./WindowSelector";
import styles from "./MetricsView.module.css";

interface Props {
  buckets: MetricsBucket[] | undefined;
  window: MetricsWindow;
}

/**
 * Band 3 -- how both have moved. Two charts, hand-drawn SVG.
 *
 * Two forms are less code than configuring a library, and the bundle stays as
 * it is. Every mark carries a `<title>`, which is a native tooltip with no
 * layer to position; a crosshair is a later refinement, not a v1 requirement.
 *
 * **There is no categorical colour on this page.** Runs are one hue because
 * completed-vs-failed as green and red separates at dE 4.9 for deuteranopia --
 * the classic red-green failure. p50 and p95 are two steps of one hue, because
 * lightness differences survive every form of colour blindness, and both are
 * direct-labelled as well as legended because the light step is 2.94:1 on
 * white. Failures live in the failure-rate tile, in a chip with words in it.
 *
 * The two things `charts.ts` gets right and a chart library would not:
 * **empty buckets draw a stub and null percentiles break the line**, and the
 * **last bucket is the current one and is drawn as partial**.
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
  const { bars, grid, ticks } = barGeometry(rows, window);

  return (
    <Card className={styles.chart}>
      <span className={styles.chartTitle}>Runs</span>
      <span className={styles.chartSub}>
        {plural(total, "run")} · {bucketWords(window)}
      </span>
      {rows.length === 0 ? (
        <p className={styles.chartEmpty}>No buckets in this window yet.</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
          width="100%"
          role="img"
          aria-label={`Runs per bucket: ${plural(total, "run")} over ${window}`}
        >
          <Axes grid={grid} ticks={ticks} />
          {bars.map((bar) => (
            <rect
              key={bar.key}
              x={bar.x}
              y={bar.y}
              width={bar.width}
              height={bar.height}
              rx={bar.empty ? 1 : 3}
              className={bar.empty ? styles.barEmpty : bar.partial ? styles.barPartial : styles.bar1}
            >
              <title>{bar.title}</title>
            </rect>
          ))}
        </svg>
      )}
    </Card>
  );
}

/**
 * Spend per bucket -- `02_costs.md` §3.2.
 *
 * The same single-hue bar treatment as runs, and **an axis entirely of its
 * own**. Runs and cost on one chart would need a second y-scale, and there is
 * no second y-scale on this page: two quantities that share no unit share no
 * axis, so if they are wanted together that is two charts, which is this.
 */
function CostChart({ buckets, window }: Props) {
  const rows = buckets ?? [];
  const total = rows.reduce((sum, bucket) => sum + bucket.cost_usd, 0);
  const { bars, grid, ticks } = costGeometry(rows, window);

  return (
    <Card className={styles.chart}>
      <span className={styles.chartTitle}>Spend</span>
      <span className={styles.chartSub}>
        {usd(total)} · {bucketWords(window)}
      </span>
      {rows.length === 0 ? (
        <p className={styles.chartEmpty}>No buckets in this window yet.</p>
      ) : (
        <svg
          viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
          width="100%"
          role="img"
          aria-label={`Spend per bucket: ${usd(total)} over ${window}`}
        >
          <Axes grid={grid} ticks={ticks} />
          {bars.map((bar) => (
            <rect
              key={bar.key}
              x={bar.x}
              y={bar.y}
              width={bar.width}
              height={bar.height}
              rx={bar.empty ? 1 : 3}
              className={bar.empty ? styles.barEmpty : bar.partial ? styles.barPartial : styles.bar1}
            >
              <title>{bar.title}</title>
            </rect>
          ))}
        </svg>
      )}
    </Card>
  );
}

function JobDurationChart({ buckets, window }: Props) {
  const rows = buckets ?? [];
  const { series, grid, ticks } = lineGeometry(rows, window);
  const measured = series.some((entry) => entry.points.length > 0);
  const latest = series.find((entry) => entry.key === "p95")?.points.slice(-1)[0];

  return (
    <Card className={styles.chart}>
      <div className={styles.chartHead}>
        <span className={styles.chartTitle}>Job duration</span>
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
        seconds · buckets with runs{latest ? "" : " · nothing measured yet"}
      </span>
      {measured ? (
        <svg
          viewBox={`0 0 ${VIEW.width} ${VIEW.height}`}
          width="100%"
          role="img"
          aria-label="Job duration in seconds, p50 and p95 per bucket"
        >
          <Axes grid={grid} ticks={ticks} />
          {series.map((entry) => {
            const line = entry.key === "p95" ? styles.line1 : styles.line2;
            const dot = entry.key === "p95" ? styles.dot1 : styles.dot2;
            const text = entry.key === "p95" ? styles.seriesLabel1 : styles.seriesLabel2;
            return (
              <g key={entry.key}>
                {entry.segments.map((d) => (
                  <path
                    key={d}
                    d={d}
                    fill="none"
                    strokeWidth={2}
                    strokeLinecap="round"
                    strokeLinejoin="round"
                    className={line}
                  />
                ))}
                {entry.points.map((point) => (
                  <circle key={point.key} cx={point.x} cy={point.y} r={3.5} className={dot}>
                    <title>{point.title}</title>
                  </circle>
                ))}
                {/* Direct-labelled as well as legended: --chart-2 is 2.94:1 on
                    white, and a visible label is the required relief. */}
                {entry.label_at ? (
                  <text
                    x={Math.min(entry.label_at.x + 8, VIEW.width - 22)}
                    y={entry.label_at.y + 3.5}
                    className={text}
                  >
                    {entry.label}
                  </text>
                ) : null}
              </g>
            );
          })}
        </svg>
      ) : (
        <p className={styles.chartEmpty}>
          No run settled in this window, so there is no percentile to draw. An empty bucket has no
          job duration — {seconds(null)} rather than zero.
        </p>
      )}
    </Card>
  );
}

function Axes({
  grid,
  ticks,
}: {
  grid: { y: number; label: string }[];
  ticks: { x: number; label: string }[];
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
