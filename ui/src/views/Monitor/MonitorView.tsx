import { useState } from "react";
import { ErrorSurface } from "../../components/ErrorSurface";
import { MetricTile } from "../../components/MetricTile";
import { PageHead } from "../../components/PageHead";
import { StateChip } from "../../components/StateChip";
import { useMonitorHost, useMonitorStages, useMonitorUpstream } from "../../hooks/useMonitor";
import { WindowSelector, bucketWords } from "./WindowSelector";
import { percent } from "../Metrics/format";
import kpi from "../Metrics/MetricsView.module.css";
import { LineChart } from "./LineChart";
import styles from "./MonitorView.module.css";
import { exhaustedChip, stageChip, topReasonLabel, topReasonTitle, usedChip } from "./status";
import type { MonitorWindow } from "./types";

/**
 * The Monitor tab: is the box healthy, not is the model good.
 *
 * Route `/monitor`. Upstream, stages and host are live from `/monitor/*`.
 */
export function MonitorView() {
  const [window, setWindow] = useState<MonitorWindow>("30m");
  const stages = useMonitorStages(window);
  const host = useMonitorHost(window);
  const up = useMonitorUpstream(window);
  const buckets = bucketWords(window);

  const upLive = up.data;
  const upChip = exhaustedChip(upLive?.exhausted_rate);
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
  const hasCalls = Boolean(upLive?.calls);
  const reasonLabel = topReasonLabel(upLive?.top_reason, upLive?.top_reason_share);

  return (
    <div className={kpi.view}>
      <PageHead
        title="Monitor"
        subtitle="This process · upstream, stages and host"
      />

      <div className={kpi.controls}>
        <WindowSelector value={window} onChange={setWindow} />
        <span className={kpi.refreshed}>window {window}</span>
      </div>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>Upstream</span>
          <span className={kpi.bandNote}>Anthropic and OpenAI through http_client · retries, not spend</span>
        </div>
        {up.error ? (
          <ErrorSurface error={up.error} onRetry={() => void up.refetch()} as="inline" />
        ) : (
          <>
            <div className={styles.tiles}>
              <MetricTile
                label="Retries / 100 calls"
                value={hasCalls ? (upLive!.retries_per_100 ?? 0).toFixed(1) : "Null"}
                sub={hasCalls ? `${upLive!.calls} calls · last 5 min` : "no outbound calls"}
              />
              <MetricTile
                label="Exhausted rate"
                value={hasCalls ? percent(upLive!.exhausted_rate) : "Null"}
                chip={upChip ? <StateChip state={upChip.state} label={upChip.words} size="sm" /> : null}
                sub="target ≤ 1%"
              />
              <MetricTile
                label={topReasonTitle(upLive?.top_reason)}
                value={reasonLabel}
                sub={
                  upLive?.top_reason
                    ? "share of retries and exhausted calls"
                    : "no retries in this window"
                }
              />
            </div>
            <div className={styles.charts}>
              <LineChart
                title="Retries / 100"
                sub={buckets}
                samples={(upLive?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.retries_per_100,
                }))}
                window={window}
                words={(v) => v.toFixed(1)}
                label="Upstream retries per 100 calls"
              />
              <LineChart
                title="Exhausted rate"
                sub={buckets}
                samples={(upLive?.series ?? []).map((row) => ({
                  bucket: row.bucket,
                  value: row.exhausted_rate,
                }))}
                window={window}
                words={(v) => percent(v)}
                label="Upstream exhausted-retry rate"
              />
            </div>
          </>
        )}
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
