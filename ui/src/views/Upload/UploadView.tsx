import { useRef, useState, type DragEvent } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, type UploadOut } from "../../api/client";
import { surfaceFor } from "../../api/errors";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { ErrorSurface } from "../../components/ErrorSurface";
import { Icon } from "../../components/Icon";
import { Label } from "../../components/Label";
import { PageHead } from "../../components/PageHead";
import { ProgressBar } from "../../components/ProgressBar";
import { useHealth } from "../../hooks/useHealth";
import { useUpload } from "../../hooks/useDocuments";
import styles from "./UploadView.module.css";

/**
 * The drop zone is the whole page until something is uploaded.
 *
 * Two refusals happen here rather than at the API, and both are the same
 * refusal the API would make: a file that is not a PDF, and a file over
 * `api_max_upload_mb`. The limit is read from `/health` rather than hardcoded,
 * so this UI and the 413 cannot disagree about what it is.
 */
export function UploadView() {
  const navigate = useNavigate();
  const health = useHealth();
  const [over, setOver] = useState(false);
  const [progress, setProgress] = useState<{ name: string; sent: number; total: number } | null>(null);
  const [rejected, setRejected] = useState<ApiError | null>(null);
  const [result, setResult] = useState<(UploadOut & { traceId: string }) | null>(null);
  const input = useRef<HTMLInputElement>(null);

  const limitMb = health.data?.max_upload_mb ?? 25;

  const mutation = useUpload(({ file, fraction }) =>
    setProgress({ name: file.name, sent: fraction * file.size, total: file.size }),
  );

  function submit(file: File | undefined) {
    if (!file) return;
    setRejected(null);
    setResult(null);

    // Refused before a byte is sent, in the API's own words and with the API's
    // own code, so the surface is the one §4 names for it either way.
    if (!file.name.toLowerCase().endsWith(".pdf") && file.type !== "application/pdf") {
      setRejected(
        new ApiError(415, "unsupported_media_type", "That is not a PDF.", "Contracts are read as PDF only."),
      );
      return;
    }
    if (file.size > limitMb * 1024 * 1024) {
      setRejected(
        new ApiError(
          413,
          "payload_too_large",
          `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB. The limit is ${limitMb} MB.`,
          "Split the contract, or raise api_max_upload_mb in settings.json.",
        ),
      );
      return;
    }

    setProgress({ name: file.name, sent: 0, total: file.size });
    mutation.mutate(file, {
      onSuccess: (uploaded) => {
        setProgress(null);
        setResult(uploaded);
      },
      onSettled: () => setProgress(null),
    });
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setOver(false);
    submit(event.dataTransfer.files[0]);
  }

  // A failure that reached the API replaces the result card; one caught above
  // renders under the zone. `surfaceFor` decides which, not this view.
  const failure = rejected ?? (mutation.error as ApiError | null);
  const inlineFailure = failure && surfaceFor(failure).placement === "inline" ? failure : null;
  const cardFailure = failure && surfaceFor(failure).placement !== "inline" ? failure : null;

  return (
    <>
      <PageHead
        title="Upload a contract"
        subtitle={`PDF up to ${limitMb} MB · parsed, chunked and indexed before it comes back with a document id`}
      />

      <div className={styles.page}>
        {progress ? (
          <Card large className={`${styles.zone} ${styles.progress}`}>
            <div className={styles.progressHead}>
              <span className={styles.progressName}>{progress.name}</span>
              <span className={styles.progressBytes}>
                {mb(progress.sent)} of {mb(progress.total)} MB
              </span>
            </div>
            <ProgressBar value={progress.total ? progress.sent / progress.total : 0} label="Upload progress" />
            <span className={styles.progressNote}>
              {progress.sent >= progress.total
                ? "Parsing, chunking and indexing. This takes a few seconds."
                : "Sending the contract."}
            </span>
          </Card>
        ) : (
          <>
            <button
              type="button"
              className={`${styles.zone} ${over ? styles.zoneOver : ""}`}
              onClick={() => input.current?.click()}
              onDragOver={(event) => {
                event.preventDefault();
                setOver(true);
              }}
              onDragLeave={() => setOver(false)}
              onDrop={onDrop}
            >
              <Icon name="upload" size={34} className={styles.zoneIcon} />
              <span className={styles.zoneTitle}>Drag and drop a contract here</span>
              <span className={styles.zoneLimit}>Limit {limitMb} MB per file · PDF only</span>
              <span className={styles.browse}>Browse files</span>
            </button>
            <input
              ref={input}
              type="file"
              accept="application/pdf,.pdf"
              className={styles.hiddenInput}
              onChange={(event) => {
                submit(event.target.files?.[0]);
                // Cleared so re-picking the same file after a failure fires
                // `change` again; without it the second attempt does nothing.
                event.target.value = "";
              }}
            />
          </>
        )}

        {inlineFailure ? (
          <div className={styles.inlineError}>
            <ErrorSurface error={inlineFailure} />
          </div>
        ) : null}

        {cardFailure ? (
          <ErrorSurface error={cardFailure} onRetry={() => input.current?.click()} />
        ) : null}

        {result && !failure ? <ResultCard result={result} onChat={() => navigate(`/documents/${result.document_id}/chat`)} onAnalyse={() => navigate(`/documents/${result.document_id}/analysis`)} /> : null}

        <div className={styles.explainer}>
          <Label>What happens on upload</Label>
          <div className={styles.steps}>
            {STEPS.map((step) => (
              <Card key={step.title} className={styles.step}>
                <span className={styles.stepTitle}>{step.title}</span>
                <span className={styles.stepBody}>{step.body}</span>
              </Card>
            ))}
          </div>
        </div>
      </div>
    </>
  );
}

/** Not a toast: it persists, because the document id it carries is what the
 *  user needs next, and the two buttons are the two things to do with it. */
function ResultCard({
  result,
  onAnalyse,
  onChat,
}: {
  result: UploadOut & { traceId: string };
  onAnalyse: () => void;
  onChat: () => void;
}) {
  return (
    <Card large>
      <div className={styles.resultHead}>
        <span className={styles.dot} />
        <span className={styles.resultTitle}>{result.filename} is ready</span>
        <span className={styles.resultElapsed}>ingested in {result.elapsed_s.toFixed(1)} s</span>
      </div>
      <div className={styles.facts}>
        <Fact label="Document id" value={String(result.document_id)} />
        <Fact label="Pages" value={String(result.pages ?? "—")} />
        {/* "Passages", never "chunks", except as a count. */}
        <Fact label="Passages" value={String(result.chunks)} />
        <Fact label="Outline from" value={result.spine_source} />
      </div>
      <div className={styles.resultActions}>
        <Button variant="primary" size="lg" onClick={onAnalyse}>
          Run compliance analysis
        </Button>
        <Button variant="secondary" size="lg" onClick={onChat}>
          Ask a question instead
        </Button>
      </div>
    </Card>
  );
}

function Fact({ label, value }: { label: string; value: string }) {
  return (
    <div className={styles.fact}>
      <Label>{label}</Label>
      <span className={styles.factValue}>{value}</span>
    </div>
  );
}

function mb(bytes: number): string {
  return (bytes / 1024 / 1024).toFixed(1);
}

const STEPS = [
  {
    title: "1 · Parse",
    body: "Text, tables and exhibits are extracted page by page, with the heading outline kept as the spine.",
  },
  {
    title: "2 · Chunk",
    body: "400-token passages with 80-token overlap, each carrying its section path and page.",
  },
  {
    title: "3 · Index",
    body: "Embeddings and full-text rows, so retrieval is hybrid and scoped to this contract alone.",
  },
  {
    title: "4 · Ready",
    body: "A document id you can analyse against the five criteria, or open a chat over.",
  },
] as const;
