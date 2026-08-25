import type { ReactNode } from "react";
import type { MetricsSummary } from "../../api/client";
import { Card } from "../../components/Card";
import { Label } from "../../components/Label";
import { DASH, percent, plural, usd } from "./format";
import styles from "./MetricsView.module.css";

/**
 * The cost breakdown -- after the spend tiles and the cost time series.
 *
 * **Not eight tiles.** Per-run p50/p95 live on the tiles and the spend chart,
 * so this band only splits the window: which model, and analysis vs chat.
 *
 * Chat is a share of billed spend, not a surface. Surfaces (`api`/`cli`) are
 * who asked for an analysis; mixing them with chat was the old card's lie.
 */
export function CostBand({ summary }: { summary: MetricsSummary | undefined }) {
  if (!summary) return null;

  const { cost_usd, tokens, cost_by_model, chat, runs } = summary;
  // A model id the price table has not learned prices at $0.00 and logs
  // `pricing.unknown_model` once. So a zero here has a known meaning, and it
  // is not "the run was free" -- which is worth the footnote below.
  const unpriced = cost_by_model.filter((row) => row.cost_usd === 0 && row.calls > 0);
  const billed = cost_usd.total + chat.cost_usd;

  return (
    <section className={styles.band}>
      <div className={styles.bandHead}>
        <span className={styles.bandTitle}>Where the money went</span>
        <span className={styles.bandNote}>
          which model billed, and analysis vs chat · per-run dollars are on the tiles
        </span>
      </div>

      <div className={styles.costGrid}>
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
          <Label>Analysis and chat</Label>
          <div className={styles.shares}>
            <Share
              name="Chat"
              value={chat.cost_usd}
              total={billed}
              sub={chat.turns === 0 ? "no turns" : plural(chat.turns, "turn")}
            >
              <Split
                rows={[
                  ["turns", chat.turns === 0 ? DASH : String(chat.turns)],
                  ["per turn", usd(chat.cost_per_turn)],
                ]}
              />
            </Share>
            <Share
              name="Analysis"
              value={cost_usd.total}
              total={billed}
              sub={plural(runs.total, "run")}
            >
              <Split
                rows={[
                  ["runs", String(runs.total)],
                  ["per run (p50)", usd(cost_usd.p50)],
                ]}
              />
            </Share>
          </div>
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
  children,
}: {
  name: string;
  value: number;
  total: number;
  sub: string;
  children?: ReactNode;
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
      {children}
    </div>
  );
}

/** `473,258`. A token count is read for its order of magnitude. */
function thousands(value: number): string {
  return value.toLocaleString("en-US");
}
