import { useEffect, useState } from "react";
import type { MetricsWindow } from "../../api/client";
import { ErrorSurface } from "../../components/ErrorSurface";
import { PageHead } from "../../components/PageHead";
import { useMetricsRuns, useMetricsSummary, useMetricsTimeseries } from "../../hooks/useMetrics";
import { CostBand } from "./CostBand";
import { ago } from "./format";
import { NowBand } from "./NowBand";
import { QualityBand } from "./QualityBand";
import { RunsBand } from "./RunsBand";
import { TrendBand } from "./TrendBand";
import { WindowSelector } from "./WindowSelector";
import styles from "./MetricsView.module.css";

/**
 * The KPI dashboard: the other half of the workspace.
 *
 * Route `/metrics`. No `:id`, no `DocumentTabs`, no document scope in the
 * sidebar -- it spans every contract, which is exactly why it cannot sit
 * inside the per-document navigation.
 *
 * Five bands: what is happening now, whether the answers can be trusted, how
 * both have moved, where the money went, and the runs themselves -- each
 * carrying the trace id that ties a number on this page to the lines that
 * produced it.
 *
 * Cost runs through three of them rather than sitting in one, which is
 * `02_costs.md` §3: **one tile, one chart, one breakdown**, plus a per-run
 * figure on every row of the runs table. Never eight cost tiles.
 *
 * **The browser draws; it does not aggregate.** Every number here is computed
 * server-side by `metrics/queries.py` -- percentiles included, over SQL rather
 * than over rows pulled into React. The three exceptions are presentational
 * and each says so where it lives: the threshold statuses (`thresholds.ts`),
 * the outcome wording (`outcome.ts`), and the chart geometry (`charts.ts`).
 *
 * **One failed query must not blank the page.** Each band renders its own
 * error where it would have been, and the others keep their numbers.
 */
export function MetricsView() {
  const [window, setWindow] = useState<MetricsWindow>("24h");
  const summary = useMetricsSummary(window);
  const timeseries = useMetricsTimeseries(window);
  const runs = useMetricsRuns(50);

  const empty = summary.data?.runs.total === 0;

  return (
    <div className={styles.view}>
      <PageHead title="Operations" subtitle="Across every document in this deployment" />

      <div className={styles.controls}>
        <WindowSelector value={window} onChange={setWindow} />
        {/* Refetching shows here and nowhere else: the numbers stay on screen
            while the next poll lands. */}
        <span className={styles.refreshed}>
          refreshed <Ago at={summary.dataUpdatedAt} /> · polling every 5 s
        </span>
      </div>

      {summary.error ? (
        <ErrorSurface error={summary.error} onRetry={() => void summary.refetch()} as="inline" />
      ) : (
        <NowBand summary={summary.data} />
      )}

      {empty ? (
        <p className={styles.footnote}>
          No runs in this window. The bands below are drawn from nothing, which is not an error —
          analyse a contract and they fill in.
        </p>
      ) : null}

      {summary.error ? null : <QualityBand summary={summary.data} />}

      <section className={styles.band}>
        <div className={styles.bandHead}>
          <span className={styles.bandTitle}>Time series</span>
        </div>
        {timeseries.error ? (
          <ErrorSurface
            error={timeseries.error}
            onRetry={() => void timeseries.refetch()}
            as="inline"
          />
        ) : (
          <TrendBand buckets={timeseries.data} window={window} />
        )}
      </section>

      {summary.error ? null : <CostBand summary={summary.data} />}

      <section className={styles.band}>
        <div className={styles.bandHead}>
          <span className={styles.bandTitle}>Recent runs</span>
          <span className={styles.bandNote}>
            newest first, across every document · the trace id is the join into{" "}
            <span className={styles.mono}>.run/app.jsonl</span>
          </span>
        </div>
        {runs.error ? (
          <ErrorSurface error={runs.error} onRetry={() => void runs.refetch()} as="inline" />
        ) : (
          <RunsBand runs={runs.data} />
        )}
      </section>
    </div>
  );
}

/**
 * "4 s ago", ticking.
 *
 * The query knows when it last answered; nothing re-renders when a second
 * passes, so this owns a one-second interval of its own. Kept to this
 * component so the tick does not re-render four bands and two charts.
 */
function Ago({ at }: { at: number }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => tick((n) => n + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  return <>{ago(at)}</>;
}
