import { useState } from "react";
import { useNavigate } from "react-router-dom";

import { ApiError, type DocumentOut } from "../../api/client";
import { Button } from "../../components/Button";
import { Card } from "../../components/Card";
import { Dialog } from "../../components/Dialog";
import { ErrorSurface } from "../../components/ErrorSurface";
import { Icon } from "../../components/Icon";
import { Label } from "../../components/Label";
import { PageHead } from "../../components/PageHead";
import { StateChip } from "../../components/StateChip";
import { EmptyState } from "../../components/EmptyState";
import { useDeleteDocument, useDocuments } from "../../hooks/useDocuments";
import { lastAnalysisWords } from "./lastAnalysis";
import styles from "./LibraryView.module.css";

export function LibraryView() {
  const navigate = useNavigate();
  const documents = useDocuments();
  const [pendingDelete, setPendingDelete] = useState<DocumentOut | null>(null);

  const rows = documents.data ?? [];

  return (
    <>
      <PageHead
        title="Document library"
        subtitle={`${rows.length} document${rows.length === 1 ? "" : "s"} · each one analysed and queried in isolation`}
      />

      {documents.error ? (
        <ErrorSurface error={documents.error} onRetry={() => void documents.refetch()} />
      ) : null}

      {rows.length === 0 && !documents.isPending && !documents.error ? (
        <EmptyState
          title="No contracts yet"
          body="Upload a PDF and it becomes a document id: something to analyse against the five criteria, or open a chat over."
          action={
            <Button variant="primary" size="lg" onClick={() => navigate("/upload")}>
              Upload a contract
            </Button>
          }
        />
      ) : null}

      {rows.length > 0 ? (
        <Card large className={styles.table}>
          <div className={styles.header}>
            <Label>Document</Label>
            <Label>Id</Label>
            <Label>Pages</Label>
            <Label>Passages</Label>
            <Label>Last analysis</Label>
            <Label className={styles.right}>Actions</Label>
          </div>

          {rows.map((row) => (
            <Row key={row.document_id} row={row} onDelete={() => setPendingDelete(row)} />
          ))}

          <p className={styles.note}>
            Each upload becomes its own document id. Retrieval, analysis and chat are scoped to one
            id, so a question about one contract can never quote another.
          </p>
        </Card>
      ) : null}

      <DeleteDialog document={pendingDelete} onClose={() => setPendingDelete(null)} />
    </>
  );
}

function Row({ row, onDelete }: { row: DocumentOut; onDelete: () => void }) {
  const navigate = useNavigate();
  const words = lastAnalysisWords(row.last_analysis);
  const running = row.last_analysis?.status === "queued" || row.last_analysis?.status === "running";

  return (
    <div className={styles.row}>
      <button
        type="button"
        className={styles.name}
        onClick={() => navigate(`/documents/${row.document_id}/analysis`)}
      >
        <span className={styles.nameText}>{row.filename}</span>
        <span className={styles.added}>added {formatAdded(row.ingested_at)}</span>
      </button>
      <span className={styles.cell}>{row.document_id}</span>
      <span className={styles.cell}>{row.pages ?? "—"}</span>
      <span className={styles.cell}>{row.chunks}</span>
      <span>
        <StateChip state={words.state} label={words.label} />
      </span>
      <div className={styles.actions}>
        <Button size="sm" onClick={() => navigate(`/documents/${row.document_id}/analysis`)}>
          Analyse
        </Button>
        <Button size="sm" onClick={() => navigate(`/documents/${row.document_id}/chat`)}>
          Chat
        </Button>
        <Button
          variant="icon"
          onClick={onDelete}
          aria-label={`Delete ${row.filename}`}
          // A control that cannot act says why, on hover and on focus. The API
          // refuses this with 409 for the same reason; this is the half that
          // happens before the request.
          disabledReason={
            running ? "An analysis of this contract is running. Cancel it first, or wait." : undefined
          }
        >
          <Icon name="trash" size={13} />
        </Button>
      </div>
    </div>
  );
}

/**
 * Delete is a confirmation, not a one-click action.
 *
 * The body names what goes with it, because "delete" is ambiguous about
 * whether the analyses go too -- and they do not: a report is the deliverable
 * and it is self-contained.
 *
 * A `409 analysis_running` from a race renders **inside the dialog** rather
 * than closing it. Closing on an error would leave the user to guess whether
 * the delete happened.
 */
function DeleteDialog({ document, onClose }: { document: DocumentOut | null; onClose: () => void }) {
  const remove = useDeleteDocument();

  function confirm() {
    if (!document) return;
    remove.mutate(document.document_id, {
      onSuccess: () => {
        remove.reset();
        onClose();
      },
    });
  }

  return (
    <Dialog
      open={document !== null}
      onClose={() => {
        remove.reset();
        onClose();
      }}
      title={document ? `Delete ${document.filename}?` : ""}
      actions={
        <>
          <Button
            onClick={() => {
              remove.reset();
              onClose();
            }}
          >
            Cancel
          </Button>
          <Button variant="destructive" onClick={confirm} disabled={remove.isPending}>
            {remove.isPending ? "Deleting…" : "Delete contract"}
          </Button>
        </>
      }
    >
      This removes the contract, its passages, its search index and its stored file. Analyses
      already run against it are kept: a report is self-contained.
      {remove.error ? (
        <div className={styles.dialogError}>
          <ErrorSurface error={remove.error as ApiError} as="inline" />
        </div>
      ) : null}
    </Dialog>
  );
}

/** `24 Aug, 04:30`, in the reader's own locale and timezone. The API sends
 *  UTC ISO-8601; a reviewer reads a clock. */
function formatAdded(iso: string): string {
  if (!iso) return "—";
  const at = new Date(iso.endsWith("Z") || iso.includes("+") ? iso : `${iso}Z`);
  if (Number.isNaN(at.getTime())) return iso;
  return at.toLocaleString(undefined, {
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });
}
