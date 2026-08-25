import { useMemo, useState } from "react";
import { MetricTile } from "../../components/MetricTile";
import { PageHead } from "../../components/PageHead";
import { StateChip } from "../../components/StateChip";
import { WindowSelector, bucketWords } from "../Metrics/WindowSelector";
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
 * Route `/monitor`. No `:id`, no document scope. Four sections, each a row of
 * tiles and the line charts of those numbers. Data is a fixture until
 * `GET /monitor/summary` and `/timeseries` exist — the layout is the step.
 */
export function MonitorView() {
  const [window, setWindow] = useState<MonitorWindow>("24h");
  const data = useMemo(() => monitorFixture(window), [window]);
  const buckets = bucketWords(window);

  const httpChip = fiveXxChip(data.http.fiveXx);
  const upChip = exhaustedChip(data.upstream.exhaustedRate);
  const stage = stageChip(data.stages.errorRate, data.stages.n);
  const rss = usedChip(data.host.rssPct);
  const disk = usedChip(data.host.diskPct);

  return (
    <div className={kpi.view}>
      <PageHead
        title="Monitor"
        subtitle="This process · HTTP, upstream, pipeline stages, host · numbers are a fixture until the API lands"
      />

      <div className={kpi.controls}>
        <WindowSelector value={window} onChange={setWindow} />
        <span className={kpi.refreshed}>fixture · window {window}</span>
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
        <div className={styles.tiles}>
          <MetricTile
            label="Worst stage"
            value={data.stages.name}
            chip={stage ? <StateChip state={stage.state} label={stage.words} size="sm" /> : null}
            sub={`${data.stages.n} samples`}
          />
          <MetricTile
            label="Error rate"
            value={percent(data.stages.errorRate)}
            sub="target ≤ 5% · n ≥ 10"
          />
          <MetricTile label="p95 latency" value={`${data.stages.p95S.toFixed(2)} s`} sub={data.stages.name} />
        </div>
        <div className={styles.charts}>
          <LineChart
            title="Stage error rate"
            sub={`${data.stages.name} · ${buckets}`}
            samples={data.series.stageError}
            window={window}
            words={(v) => percent(v)}
            label={`Error rate for ${data.stages.name}`}
          />
          <LineChart
            title="Stage p95"
            sub={`${data.stages.name} · seconds`}
            samples={data.series.stageP95}
            window={window}
            words={(v) => `${v.toFixed(2)} s`}
            label={`p95 latency for ${data.stages.name}`}
          />
        </div>
      </section>

      <section className={kpi.band}>
        <div className={kpi.bandHead}>
          <span className={kpi.bandTitle}>Host</span>
          <span className={kpi.bandNote}>
            used % · plenty under 20%, tight over 90% · no pager
          </span>
        </div>
        <div className={styles.tiles2}>
          <MetricTile
            label="Memory"
            value={percent(data.host.rssPct, 0)}
            chip={<StateChip state={rss.state} label={rss.words} size="sm" />}
            sub={`${Math.round(data.host.rssMb)} MB this process`}
          />
          <MetricTile
            label="Disk"
            value={percent(data.host.diskPct, 0)}
            chip={<StateChip state={disk.state} label={disk.words} size="sm" />}
            sub={`${data.host.diskGb.toFixed(1)} GB of ${data.host.diskTotalGb} GB`}
          />
        </div>
        <div className={styles.charts}>
          <LineChart
            title="Memory"
            sub={buckets}
            samples={data.series.rss}
            window={window}
            words={(v) => percent(v, 0)}
            label="Memory this process is using, as a share of the machine"
          />
          <LineChart
            title="Disk"
            sub={buckets}
            samples={data.series.disk}
            window={window}
            words={(v) => percent(v, 0)}
            label="Disk used percent"
          />
        </div>
      </section>
    </div>
  );
}
