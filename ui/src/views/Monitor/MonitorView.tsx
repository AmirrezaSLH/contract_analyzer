import { useMemo, useState } from "react";
import { ErrorSurface } from "../../components/ErrorSurface";
import { MetricTile } from "../../components/MetricTile";
import { PageHead } from "../../components/PageHead";
import { StateChip } from "../../components/StateChip";
import { useMonitorHost, useMonitorStages } from "../../hooks/useMonitor";
import { WindowSelector, bucketWords } from "./WindowSelector";
import { millis, percent } from "../Metrics/format";
import kpi from "../Metrics/MetricsView.module.css";
import { LineChart } from "./LineChart";
import styles from "./MonitorView.module.css";
import { monitorFixture } from "./fixture";
import { exhaustedChip, fiveXxChip, stageChip, usedChip } from "./status";
import type { MonitorWindow } from "./types";

/**
 * The Monitor tab: is the box healthy, not is the model good.
 *
 * Route `/monitor`. Stages and host are live. HTTP and upstream are still a
 * fixture until those endpoints exist.
 */
export function MonitorView() {
  const [window, setWindow] = useState<MonitorWindow>("30m");
  const data = useMemo(() => monitorFixture(window), [window]);
  const stages = useMonitorStages(window);
  const host = useMonitorHost(window);
  const buckets = bucketWords(window);

  const httpChip = fiveXxChip(data.http.fiveXx);
  const upChip = exhaustedChip(data.upstream.exhaustedRate);
  const live = stages.data;
  const stage = live ? stageChip(live.error_rate, live.n) : null;
  const snap = host.data;
  const rss = usedChip(snap?.rss_pct);
  const disk = usedChip(snap?.disk_used_pct);
  const hasStage = Boolean(live?.name);
  const hostGrain = snap?.bucket ? `${snap.bucket} samples` : buckets;
  const stageValue = hasStage
    ? `${live!.name} · ${live!.errors} failed`
    : "Null";

  return (
    <div className={kpi.view}>
      <PageHead
        title="Monitor"
        subtitle="This process · stages and host are live · HTTP and upstream are a fixture until the API lands"
      />

      <div className={kpi.controls}>
        <WindowSelector value={window} onChange={setWindow} />
        <span className={kpi.refreshed}>window {window}</span>
      </div>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>HTTP</span>
          <span className={kpi.bandNote}>
            last 5 min on the tiles · {buckets} on the charts · static and SSE omitted
          </span>
        </div>
        <div className={styles.tiles}>
          <MetricTile label="Requests / min" value={data.http.rpm.toFixed(1)} sub="API + /health" />
          <MetricTile
            label="5xx rate"
            value={percent(data.http.fiveXx)}
            chip={httpChip ? <StateChip state={httpChip.state} label={httpChip.words} size="sm" /> : null}
            sub="target ≤ 1%"
          />
          <MetricTile label="p95 latency" value={millis(data.http.p95Ms)} sub="completed API calls" />
        </div>
        <div className={styles.charts}>
          <LineChart
            title="Requests / min"
            sub={buckets}
            samples={data.series.httpRpm}
            window={window}
            words={(v) => v.toFixed(1)}
            label="HTTP requests per minute"
          />
          <LineChart
            title="5xx rate"
            sub={buckets}
            samples={data.series.httpFiveXx}
            window={window}
            words={(v) => percent(v)}
            label="HTTP 5xx rate"
          />
          <LineChart
            title="p95 latency"
            sub={`${buckets} · milliseconds`}
            samples={data.series.httpP95}
            window={window}
            words={(v) => millis(v)}
            label="HTTP p95 latency in milliseconds"
          />
        </div>
      </section>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>Upstream</span>
          <span className={kpi.bandNote}>Anthropic and OpenAI through http_client · retries, not spend</span>
        </div>
        <div className={styles.tiles}>
          <MetricTile
            label="Retries / 100 calls"
            value={data.upstream.retriesPer100.toFixed(1)}
            sub="last 5 min"
          />
          <MetricTile
            label="Exhausted rate"
            value={percent(data.upstream.exhaustedRate)}
            chip={upChip ? <StateChip state={upChip.state} label={upChip.words} size="sm" /> : null}
            sub="target ≤ 1%"
          />
          <MetricTile label="Top reason" value={data.upstream.topReason} sub="right now" />
        </div>
        <div className={styles.charts}>
          <LineChart
            title="Retries / 100"
            sub={buckets}
            samples={data.series.retries}
            window={window}
            words={(v) => v.toFixed(1)}
            label="Upstream retries per 100 calls"
          />
          <LineChart
            title="Exhausted rate"
            sub={buckets}
            samples={data.series.exhausted}
            window={window}
            words={(v) => percent(v)}
            label="Upstream exhausted-retry rate"
          />
        </div>
      </section>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>Stages</span>
          <span className={kpi.bandNote}>
            where it broke, not whether · worst span name over the last 5 min
          </span>
        </div>
        {stages.error ? (
          <ErrorSurface error={stages.error} onRetry={() => void stages.refetch()} as="inline" />
        ) : (
          <>
            <div className={styles.tiles}>
              <MetricTile
                label="Worst stage"
                value={stageValue}
                chip={stage ? <StateChip state={stage.state} label={stage.words} size="sm" /> : null}
                sub={hasStage ? `${live!.n} samples` : "no spans in this window"}
              />
              <MetricTile
                label="Error rate"
                value={hasStage ? percent(live!.error_rate) : "Null"}
                sub="target ≤ 5% · n ≥ 10"
              />
              <MetricTile
                label="Total errors"
                value={live?.errors_total == null ? "Null" : String(live.errors_total)}
                sub="all named stages in this window"
              />
            </div>
            <div className={styles.charts}>
              <LineChart
                title="Stage error rate"
                sub={`${hasStage ? live!.name : "Null"} · ${buckets}`}
                samples={(live?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.error_rate,
                }))}
                window={window}
                words={(v) => percent(v)}
                label={`Error rate for ${hasStage ? live!.name : "Null"}`}
              />
              <LineChart
                title="Total errors"
                sub={`all named stages · ${buckets}`}
                samples={(live?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.errors_total,
                }))}
                window={window}
                words={(v) => String(Math.round(v))}
                label="Error count across named pipeline stages"
              />
            </div>
          </>
        )}
      </section>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>Host</span>
          <span className={kpi.bandNote}>
            used % · plenty under 20%, tight over 90% · no pager
          </span>
        </div>
        {host.error ? (
          <ErrorSurface error={host.error} onRetry={() => void host.refetch()} as="inline" />
        ) : (
          <>
            <div className={styles.tiles2}>
              <MetricTile
                label="Memory"
                value={snap?.rss_pct == null ? "Null" : percent(snap.rss_pct, 0)}
                chip={rss ? <StateChip state={rss.state} label={rss.words} size="sm" /> : null}
                sub={
                  snap?.rss_mb == null
                    ? "how much RAM this process is using"
                    : `${Math.round(snap.rss_mb)} MB this process`
                }
              />
              <MetricTile
                label="Disk"
                value={snap?.disk_used_pct == null ? "Null" : percent(snap.disk_used_pct, 0)}
                chip={disk ? <StateChip state={disk.state} label={disk.words} size="sm" /> : null}
                sub={
                  snap?.disk_used_gb == null || snap.disk_total_gb == null
                    ? "database volume"
                    : `${snap.disk_used_gb.toFixed(1)} GB of ${snap.disk_total_gb.toFixed(0)} GB`
                }
              />
            </div>
            <div className={styles.charts}>
              <LineChart
                title="Memory"
                sub={hostGrain}
                samples={(snap?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.rss_pct,
                }))}
                window={window}
                words={(v) => percent(v, 0)}
                label="Memory this process is using, as a share of the machine"
              />
              <LineChart
                title="Disk"
                sub={hostGrain}
                samples={(snap?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.disk_used_pct,
                }))}
                window={window}
                words={(v) => percent(v, 0)}
                label="Disk used percent"
              />
            </div>
          </>
        )}
      </section>
    </div>
  );
}
