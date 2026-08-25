import { useState } from "react";
import type { RunRow } from "../../api/client";
import { Card } from "../../components/Card";
import { Label } from "../../components/Label";
import { StateChip } from "../../components/StateChip";
import { DASH, seconds, startedAt, usd } from "./format";
import { runOutcome } from "./outcome";
import styles from "./MetricsView.module.css";

/**
 * Band 4 -- the runs themselves, newest first, across every document.
 *
 * `GET /analyses` is per document on purpose, so this list cannot be assembled
 * from it; `GET /metrics/runs` is the global one, and it does not carry
 * `report_json` because a runs table wants none of thirty kilobytes of report
 * per row.
 *
 * **The trace id is why this table exists.** It is the join from a number on
 * this page to the lines in `.run/app.jsonl` that produced it, which is what
 * makes the live log walkthrough work. It is rendered in the mono face and it
 * is click-to-copy, because a reviewer reading it is about to paste it into a
 * grep.
 */
export function RunsBand({ runs }: { runs: RunRow[] | undefined }) {
  if (!runs) return <Card className={styles.table}><div className={styles.note}>Loading runs…</div></Card>;

  return (
    <Card className={styles.table}>
      <div className={styles.header}>
        <Label>Started</Label>
        <Label>Document</Label>
        <Label>Outcome</Label>
        <Label className={styles.rowWide}>Job duration</Label>
        <Label className={styles.rowWide}>Cost</Label>
        <Label className={styles.rowWide}>Trace</Label>
      </div>

      {runs.map((run) => (
        <Row key={run.analysis_id} run={run} />
      ))}

      <p className={styles.note}>
        {runs.length === 0
          ? "No runs recorded yet. A run appears here the moment one settles, whichever surface asked for it."
          : "Every row carries the trace id its run was made under, so a number on this page can be followed to the lines that produced it."}
      </p>
    </Card>
  );
}

function Row({ run }: { run: RunRow }) {
  const outcome = runOutcome(run);
  return (
    <div className={styles.row}>
      <span className={styles.cell}>{startedAt(run.created_at)}</span>
      <span className={styles.filename} title={run.filename}>
        {run.filename || DASH}
      </span>
      <span>
        <StateChip state={outcome.tone} label={outcome.words} size="sm" />
      </span>
      <span className={`${styles.cell} ${styles.rowWide}`}>{seconds(run.job_duration_s)}</span>
      <span className={`${styles.cell} ${styles.rowWide}`}>{usd(run.cost_usd)}</span>
      <TraceId value={run.trace_id} />
    </div>
  );
}

/** Click to copy. The confirmation is the word, not a toast: this page has no
 *  toast layer and one id copied does not deserve one. */
function TraceId({ value }: { value: string | null | undefined }) {
  const [copied, setCopied] = useState(false);

  if (!value) return <span className={`${styles.trace} ${styles.rowWide}`}>{DASH}</span>;

  return (
    <button
      type="button"
      className={`${styles.trace} ${styles.rowWide} ${copied ? styles.traceCopied : ""}`}
      title={`Copy ${value}`}
      onClick={() => {
        // `clipboard` is undefined on an insecure origin, and a demo served
        // over plain http is exactly where this would be read aloud instead.
        void navigator.clipboard?.writeText(value).then(
          () => {
            setCopied(true);
            window.setTimeout(() => setCopied(false), 1200);
          },
          () => undefined,
        );
      }}
    >
      {copied ? "copied" : value}
    </button>
  );
}
