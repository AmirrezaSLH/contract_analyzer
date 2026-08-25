import type { MetricsSummary } from "../../api/client";
import { Card } from "../../components/Card";
import { Label } from "../../components/Label";
import { StateChip } from "../../components/StateChip";
import { DASH, millis, percent, plural, usd } from "./format";
import { costTailRatio, statusOf, THRESHOLDS } from "./thresholds";
import styles from "./MetricsView.module.css";

/**
 * The cost breakdown -- the third of the three things `02_costs.md` §3 asks
 * for, after the Spend tile and the cost trend chart.
 *
 * **One tile, one chart, one breakdown. Not eight tiles.** The rule this band
 * is built on is §1: *dollars on the tile, tokens and calls behind it*. Cost
 * is the only family where three numbers describe the same event -- calls,
 * tokens, dollars -- and promoting all three to tiles says the same thing
 * three times. The dollar is what a budget is written in; the tokens explain
 * the dollar; the call count explains retries and caps. So all three are
 * stored, one is displayed, and this is the "behind it".
 *
 * Three slices, each answering a different question:
 *
 *   * **By model** -- from `agent.call` spans, so it covers analysis *and*
 *     chat in one pass. This is why per-model cost waited for `spans` instead
 *     of being mined out of `report_json`: that would have been analysis-only.
 *   * **By surface** -- who asked. `ui`, `cli`, `mcp`, `api`. A free
 *     `GROUP BY`, and **not the same slice as chat-vs-analyze**: every row in
 *     it is an analysis, because chat writes no run row by design.
 *   * **Chat** -- which is exactly why it is separate: chat is stateless and
 *     is queried as `spans WHERE name = 'chat'`. Giving it a row in `analyses`
 *     would have meant every analysis KPI needing `WHERE surface != 'chat'`
 *     forever.
 *
 * Two honest-emptiness rules, both from `03_data_contract.md` trap 7. An empty
 * `cost_by_model` is a real answer -- a window with no calls, or a database
 * whose spans predate the table -- and says so rather than drawing a blank
 * box. And its **token counts are best-effort**: cost is shown, tokens are
 * context.
 */
export function CostBand({ summary }: { summary: MetricsSummary | undefined }) {
  if (!summary) return null;

  const { cost_usd, tokens, surfaces, cost_by_model, chat, runs } = summary;
  const tail = costTailRatio(cost_usd.p50, cost_usd.p95);
  const tailStatus = statusOf(tail, THRESHOLDS.costTail);
  // A model id the price table has not learned prices at $0.00 and logs
  // `pricing.unknown_model` once. So a zero here has a known meaning, and it
  // is not "the run was free" -- which is worth the footnote below.
  const unpriced = cost_by_model.filter((row) => row.cost_usd === 0 && row.calls > 0);

  return (
    <section className={styles.band}>
      <div className={styles.bandHead}>
        <span className={styles.bandTitle}>Where the money went</span>
        <span className={styles.bandNote}>
          dollars on the tile, tokens and calls behind it · per-run cost is in the runs table, row by
          row
        </span>
      </div>

      <div className={styles.costGrid}>
        <Card className={styles.costCard}>
          <div className={styles.meterHead}>
            <Label>Per run</Label>
            <StateChip
              state={tailStatus.tone}
              label={tail === null ? "not measured" : `p95 ${tail.toFixed(1)}× p50`}
              size="sm"
            />
          </div>
          <Split rows={[
            ["p50", usd(cost_usd.p50)],
            ["p95", usd(cost_usd.p95)],
            ["mean", usd(cost_usd.mean)],
          ]} />
          <span className={styles.meterNote}>
            Percentiles, not the average — {THRESHOLDS.costTail.action} The mean is here as context
            and carries no threshold of its own.
          </span>
        </Card>

        <Card className={styles.costCard}>
          <Label>By model</Label>
          {cost_by_model.length === 0 ? (
            <span className={styles.meterNote}>
              No <span className={styles.mono}>agent.call</span> spans in this window. That is an
              answer, not a gap: this deployment's spans may predate the table, or nothing called a
              model. The window total above is still right — it comes from{" "}
              <span className={styles.mono}>analyses</span>.
            </span>
          ) : (
            <>
              <div className={styles.shares}>
                {cost_by_model.map((row) => (
                  <Share
                    key={row.model ?? "unknown"}
                    name={row.model ?? "unknown model"}
                    value={row.cost_usd}
                    total={cost_usd.total}
                    sub={`${plural(row.calls, "call")}${
                      row.input_tokens + row.output_tokens > 0
                        ? ` · ${thousands(row.input_tokens + row.output_tokens)} tokens`
                        : ""
                    }`}
                  />
                ))}
              </div>
              <span className={styles.meterNote}>
                From <span className={styles.mono}>agent.call</span> spans, so this covers analysis
                and chat together. Token counts are best-effort; the dollar is the number to read.
              </span>
            </>
          )}
        </Card>

        <Card className={styles.costCard}>
          <Label>By surface, and chat</Label>
          <div className={styles.shares}>
            {surfaces.map((row) => (
              <Share
                key={row.surface ?? "unknown"}
                name={row.surface ?? "unknown"}
                value={row.cost_usd}
                total={cost_usd.total}
                sub={plural(row.runs, "run")}
              />
            ))}
          </div>
          <Split rows={[
            ["chat turns", chat.turns === 0 ? DASH : String(chat.turns)],
            ["chat cost", chat.turns === 0 ? DASH : usd(chat.cost_usd)],
            ["per turn", usd(chat.cost_per_turn)],
            ["chat p95 duration", millis(chat.latency_ms.p95)],
          ]} />
          <span className={styles.meterNote}>
            Every surface above is an analysis — chat is counted separately because it writes no run
            row, which is what keeps every analysis KPI from needing to exclude it.
          </span>
        </Card>
      </div>

      <p className={styles.footnote}>
        {plural(tokens.input, "input token")} · {plural(tokens.output, "output token")} ·{" "}
        {plural(tokens.tool_calls, "tool call")} over {plural(runs.total, "run")}. Tokens explain the
        dollar and calls explain the retries; neither gets a tile.
        {unpriced.length > 0 ? (
          <>
            {" "}
            <strong>
              {unpriced.map((row) => row.model).join(", ")} priced at $0.00 across{" "}
              {plural(unpriced.reduce((sum, row) => sum + row.calls, 0), "call")}.
            </strong>{" "}
            That is a model id the price table has not learned, not a free run — see{" "}
            <span className={styles.mono}>generation/pricing.py</span>.
          </>
        ) : null}{" "}
        Embedding cost is captured on the <span className={styles.mono}>ingest.embed</span> span and
        deliberately not tiled: embedding the sample contract costs about $0.0002, four orders of
        magnitude under the analysis it enables.
      </p>
    </section>
  );
}

/** A labelled row of numbers. Tabular figures, so the column reads down. */
function Split({ rows }: { rows: [string, string][] }) {
  return (
    <dl className={styles.split}>
      {rows.map(([label, value]) => (
        <div key={label} className={styles.splitRow}>
          <dt className={styles.splitLabel}>{label}</dt>
          <dd className={styles.splitValue}>{value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** One slice, as a name, a dollar figure and a share bar.
 *
 *  A bar rather than a pie: a share is a length comparison, and lengths on a
 *  common baseline are the one encoding everybody reads the same way. One hue
 *  throughout -- the slices are not categories that mean anything, they are
 *  the same quantity split up. */
function Share({
  name,
  value,
  total,
  sub,
}: {
  name: string;
  value: number;
  total: number;
  sub: string;
}) {
  const share = total > 0 ? value / total : 0;
  return (
    <div className={styles.share}>
      <div className={styles.shareHead}>
        <span className={styles.shareName} title={name}>
          {name}
        </span>
        <span className={styles.shareValue}>{usd(value)}</span>
      </div>
      <div className={styles.bar}>
        <div className={`${styles.fill} ${styles.fillChart}`} style={{ width: `${share * 100}%` }} />
      </div>
      <span className={styles.shareSub}>
        {sub} · {total > 0 ? percent(share, 0) : DASH} of spend
      </span>
    </div>
  );
}

/** `473,258`. A token count is read for its order of magnitude. */
function thousands(value: number): string {
  return value.toLocaleString("en-US");
}
