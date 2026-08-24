import { useEffect, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";

import type { Analysis, ComplianceResult, CriterionProgress } from "../../api/client";
import { Banner } from "../../components/Banner";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { DocumentTabs } from "../../components/DocumentTabs";
import { EmptyState } from "../../components/EmptyState";
import { ErrorSurface } from "../../components/ErrorSurface";
import { MetricTile } from "../../components/MetricTile";
import { NO_KEY_REASON, NoKeyBanner } from "../../components/NoKeyBanner";
import { PageHead } from "../../components/PageHead";
import { ProgressBar } from "../../components/ProgressBar";
import { StateChip } from "../../components/StateChip";
import { useCriteria } from "../../hooks/useCriteria";
import { useDocument } from "../../hooks/useDocuments";
import { useHealth } from "../../hooks/useHealth";
import { useAnalysis, useCancelAnalysis, useCreateAnalysis, isTerminal } from "../../hooks/useAnalysis";
import { CriterionRow } from "./CriterionRow";
import { needsReviewCount, overallState, quoteCounts } from "./overall";
import styles from "./AnalysisView.module.css";

/**
 * Five states, one card that mutates.
 *
 * Never five layouts: the header, the progress and the criterion list keep
 * their places as a run moves from queued to running to done, so nothing jumps
 * under the reader while they are watching it.
 */
export function AnalysisView() {
  const { id } = useParams();
  const documentId = Number(id);
  const navigate = useNavigate();

  const health = useHealth();
  const criteria = useCriteria();
  const document = useDocument(Number.isInteger(documentId) ? documentId : null);
  const create = useCreateAnalysis();
  const cancel = useCancelAnalysis();

  // Derived, never stored. A local Map<documentId, analysisId> would be a
  // second source of truth for a fact the server already owns.
  const analysisId = create.data?.analysis_id ?? document.data?.last_analysis?.analysis_id ?? null;
  const analysis = useAnalysis(analysisId);

  // Which criterion is open. Resets on document change, which is correct.
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => setOpen(null), [documentId]);

  if (document.error) return <ErrorSurface error={document.error} />;

  const doc = document.data;
  const run = analysis.data ?? null;
  const results = (run?.report?.results ?? []) as ComplianceResult[];
  const finished = run?.status === "done" || run?.status === "cancelled";
  const keyless = health.data?.key_present === false;

  const start = (rerun = false) => create.mutate({ documentId, rerun });

  return (
    <>
      <PageHead
        title={doc?.filename ?? "Analysis"}
        subtitle={subtitleFor(run, doc?.pages ?? null, doc?.chunks ?? 0)}
        actions={
          finished && run?.report ? (
            <>
              <Button onClick={() => start(true)} disabled={create.isPending}>
                Re-run
              </Button>
              <Button variant="primary" onClick={() => exportReport(run)}>
                Export JSON
              </Button>
            </>
          ) : null
        }
      />

      <NoKeyBanner />
      {Number.isInteger(documentId) ? <DocumentTabs documentId={documentId} /> : null}

      <div className={styles.page}>
        {create.error ? (
          <ErrorSurface error={create.error} onRetry={() => start()} />
        ) : null}

        {/* An error never destroys work: a failed submission leaves whatever
            report was already on screen exactly where it was. */}
        {analysis.error && !run ? (
          <ErrorSurface error={analysis.error} onRetry={() => void analysis.refetch()} />
        ) : null}

        {run?.status === "failed" ? (
          <Banner
            tone="error"
            title="This analysis failed."
            // Verbatim. A generic message throws away the only information on
            // screen about what went wrong.
            body={run.error ?? undefined}
            traceId={run.trace_id ?? undefined}
            action={
              <Button variant="secondary" size="sm" onClick={() => start(true)}>
                Re-run
              </Button>
            }
          />
        ) : null}

        {run?.status === "interrupted" ? (
          <Banner
            tone="warn"
            title="This analysis was interrupted."
            hint="The process running it went away before it finished. Nothing refused; run it again."
            traceId={run.trace_id ?? undefined}
            action={
              <Button variant="secondary" size="sm" onClick={() => start(true)}>
                Run again
              </Button>
            }
          />
        ) : null}

        {run?.status === "cancelled" ? (
          <Banner
            tone="info"
            title={`Cancelled after ${results.length} of ${(run.report?.skipped?.length ?? 0) + results.length} criteria.`}
            hint="The criteria that finished are below. The rest were never started."
            traceId={run.trace_id ?? undefined}
            action={
              <Button variant="secondary" size="sm" onClick={() => start(true)}>
                Run again
              </Button>
            }
          />
        ) : null}

        {/* a. No analysis yet. */}
        {!run && !analysis.isPending && !create.isPending ? (
          <EmptyState
            title={`${doc?.filename ?? "This contract"} has not been analysed yet`}
            body="A run answers all five compliance questions against this contract alone. It takes about a minute and costs roughly a dollar, so it is never started for you."
            action={
              <Button
                variant="primary"
                size="lg"
                onClick={() => start()}
                disabledReason={keyless ? NO_KEY_REASON : undefined}
              >
                Run compliance analysis
              </Button>
            }
          />
        ) : null}

        {/* b and c. Queued and running: the same card. */}
        {run && (run.status === "queued" || run.status === "running") ? (
          <RunCard
            run={run}
            titles={titlesOf(criteria.data)}
            workers={health.data?.api_workers ?? 2}
            onCancel={() => cancel.mutate(run.analysis_id)}
            cancelling={cancel.isPending}
          />
        ) : null}

        {/* d. Done -- and a cancelled run's partial report, which is the same
            report with fewer results in it. */}
        {finished && results.length > 0 ? (
          <>
            <div className={styles.tiles}>
              <MetricTile label="Overall" value={<StateChip state={overallState(results)} />} />
              <MetricTile
                label="Mean confidence"
                value={(run.report?.totals?.mean_confidence ?? 0).toFixed(2)}
              />
              <MetricTile
                label="Quotes verified"
                value={`${quoteCounts(results).verified} / ${quoteCounts(results).total}`}
              />
              <MetricTile label="Needs review" value={String(needsReviewCount(results))} />
            </div>

            <div className={styles.criteria}>
              {results.map((result, index) => (
                <CriterionRow
                  key={result.criterion_id}
                  index={index + 1}
                  result={result}
                  criterion={criteria.data?.find((c) => c.id === result.criterion_id)}
                  open={open === result.criterion_id}
                  // Opening a row closes the previously open one.
                  onToggle={() =>
                    setOpen((current) => (current === result.criterion_id ? null : result.criterion_id))
                  }
                />
              ))}
            </div>

            <div className={styles.reportFoot}>
              <span>analysis {run.analysis_id}</span>
              {run.trace_id ? <span className={styles.trace}>trace {run.trace_id}</span> : null}
              <span>{run.report?.totals?.tool_calls ?? 0} tool calls</span>
              <span>
                {run.report?.totals?.input_tokens ?? 0} in · {run.report?.totals?.output_tokens ?? 0} out
              </span>
            </div>
          </>
        ) : null}

        {finished && results.length === 0 && run?.status === "cancelled" ? (
          <EmptyState
            title="Nothing finished before this run was cancelled"
            body="No criterion had produced a verdict yet, so there is no partial report to show."
            action={
              <Button variant="primary" size="lg" onClick={() => navigate(0)}>
                Reload
              </Button>
            }
          />
        ) : null}
      </div>
    </>
  );
}

/** The queued and running card. */
function RunCard({
  run,
  titles,
  workers,
  onCancel,
  cancelling,
}: {
  run: Analysis;
  titles: Map<string, string>;
  workers: number;
  onCancel: () => void;
  cancelling: boolean;
}) {
  const running = run.status === "running";
  const rows = run.criteria ?? [];
  const total = run.progress?.total || rows.length || 5;
  const done = run.progress?.done ?? 0;
  const inFlight = rows.filter((row) => row.status === "running");

  return (
    <Card large>
      <div className={styles.runHead}>
        <span className={`${styles.dot} ${running ? styles.dotRunning : styles.dotQueued}`} />
        <span className={styles.runHeadline}>
          {running ? `Analysing ${total} criteria` : "Queued"}
        </span>
        <span className={styles.runSub}>
          {running ? `started ${clock(run.started_at)}` : "waiting for a worker"}
        </span>
        <Button size="sm" onClick={onCancel} disabled={cancelling}>
          {cancelling ? "Cancelling…" : "Cancel"}
        </Button>
      </div>

      <div className={styles.stage}>
        {/* Announced, so a screen reader hears progress rather than silence. */}
        <div className={styles.stageLine} aria-live="polite">
          <span>
            {running
              ? inFlight.length > 0
                ? `criterion ${done + 1} of ${total} · ${inFlight.map((r) => r.id).join(", ")}`
                : `criterion ${done + 1} of ${total}`
              : `Waiting for a worker — ${workers} ${workers === 1 ? "analysis runs" : "analyses run"} at a time`}
          </span>
          <span>
            {done} of {total} criteria
          </span>
        </div>
        <ProgressBar value={total ? done / total : 0} label="Analysis progress" />
      </div>

      <div className={styles.rows}>
        {rows.map((row, index) => (
          <ProgressRow key={row.id} index={index + 1} row={row} title={titles.get(row.id) ?? row.id} />
        ))}
      </div>

      <div className={styles.runFoot}>
        <span>elapsed {elapsed(run.started_at ?? run.created_at)}</span>
        {/* Cost is not known until the report exists: the runner totals a run
            when it finishes, and inventing a partial figure would be worse
            than saying so. */}
        <span>cost so far —</span>
        <span>
          {workers} workers · {total} criteria in parallel
        </span>
        {run.trace_id ? <span className={styles.trace}>trace {run.trace_id}</span> : null}
      </div>
    </Card>
  );
}

function ProgressRow({
  index,
  row,
  title,
}: {
  index: number;
  row: CriterionProgress;
  title: string;
}) {
  const done = row.status === "done";
  const active = row.status === "running";
  const skipped = row.status === "skipped";
  return (
    <div className={styles.row}>
      <span
        className={`${styles.rowDot} ${
          done ? styles.rowDotDone : active ? styles.rowDotActive : styles.rowDotWaiting
        }`}
      />
      <span className={`${styles.rowName} ${done || active ? "" : styles.rowWaiting}`}>
        {index} · {title}
      </span>
      <span className={`${styles.rowState} ${done ? styles.rowStateDone : ""}`}>
        {done ? row.state ?? "done" : active ? "retrieving…" : skipped ? "skipped" : "waiting"}
      </span>
      <span className={styles.rowNum}>{row.confidence != null ? row.confidence.toFixed(2) : "—"}</span>
      <span className={styles.rowLat}>{row.latency_s != null ? `${row.latency_s.toFixed(1)} s` : "—"}</span>
    </div>
  );
}

function titlesOf(criteria: { id: string; requirement: string }[] | undefined): Map<string, string> {
  return new Map((criteria ?? []).map((c) => [c.id, c.requirement]));
}

function subtitleFor(run: Analysis | null, pages: number | null, chunks: number): string {
  if (!run) return `${pages ?? "—"} pages · ${chunks} passages · not analysed yet`;
  if (run.status === "done" && run.report) {
    const totals = run.report.totals;
    return `Analysed ${when(run.completed_at)} · ${totals?.criteria ?? 0} criteria · ${(totals?.latency_s ?? 0).toFixed(1)} s · $${(totals?.cost_usd ?? 0).toFixed(2)}`;
  }
  if (run.status === "queued") return `Analysis ${run.analysis_id.slice(0, 8)} · queued · ${pages ?? "—"} pages`;
  if (run.status === "running")
    return `Analysis ${run.analysis_id.slice(0, 8)} · running · started ${clock(run.started_at)}`;
  return `Analysis ${run.analysis_id.slice(0, 8)} · ${run.status}`;
}

function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const at = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  return Number.isNaN(at.getTime()) ? null : at;
}

function clock(iso: string | null | undefined): string {
  return parse(iso)?.toLocaleTimeString(undefined, { hour12: false }) ?? "—";
}

function when(iso: string | null | undefined): string {
  const at = parse(iso);
  return at
    ? at.toLocaleString(undefined, { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })
    : "—";
}

function elapsed(iso: string | null | undefined): string {
  const at = parse(iso);
  if (!at) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - at.getTime()) / 1000));
  if (seconds < 60) return `${seconds} s`;
  return `${Math.floor(seconds / 60)} m ${String(seconds % 60).padStart(2, "0")} s`;
}

/** The report exactly as the API returned it -- the same bytes `make analyze`
 *  writes to disk, so an exported file validates as an `AnalysisReport`. */
function exportReport(run: Analysis): void {
  const blob = new Blob([JSON.stringify(run.report, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `analysis-${run.analysis_id}.json`;
  link.click();
  URL.revokeObjectURL(url);
}

export { isTerminal };
