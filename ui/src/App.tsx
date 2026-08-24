import { Link, Outlet, useMatch, useNavigate } from "react-router-dom";
import { Button } from "./components/Button";
import { Icon } from "./components/Icon";
import { Label } from "./components/Label";
import { useDocument, useDocuments } from "./hooks/useDocuments";
import { useHealth } from "./hooks/useHealth";
import { lastAnalysisWords } from "./views/Library/lastAnalysis";
import styles from "./App.module.css";

/**
 * The shell: the sidebar is application navigation, the outlet is the view.
 *
 * The sidebar carries scope -- which contract everything else is about -- and
 * it reads that scope **from the URL**, never the other way round. `:id` is the
 * single source of truth: it is copyable, linkable and survives a reload, and
 * a second copy of it in React state would be a second thing to keep in sync.
 */
export function App() {
  const documentId = useActiveDocumentId();
  const documents = useDocuments();
  const active = useDocument(documentId);
  const health = useHealth();
  const navigate = useNavigate();
  const onLibrary = useMatch("/library") !== null;

  const doc = active.data;
  const rows = documents.data ?? [];

  return (
    <div className={styles.shell}>
      <nav className={styles.sidebar} aria-label="Application">
        <div className={styles.brand}>
          <span className={styles.brandName}>Contract Analyzer</span>
          <span className={styles.brandSub}>Compliance review workspace</span>
        </div>

        <div className={styles.group}>
          <Label>Active document</Label>
          <button
            type="button"
            className={styles.active}
            onClick={() => navigate("/library")}
            title={doc ? doc.filename : "Pick a contract from the library"}
          >
            <span className={styles.activeName}>
              {doc ? doc.filename : "No document selected"}
            </span>
            <Icon name="chevron" size={10} weight={1.5} className={styles.activeChevron} />
          </button>
          <span className={styles.meta}>
            {doc
              ? `id ${doc.document_id} · ${doc.pages ?? "—"} pages · ${doc.chunks} passages`
              : "Upload a contract, or choose one from the library"}
          </span>
        </div>

        <Button variant="primary" size="lg" block onClick={() => navigate("/upload")}>
          Upload a contract
        </Button>

        <div className={styles.navGroup}>
          <button
            type="button"
            className={`${styles.navRow} ${onLibrary ? styles.navSelected : ""}`}
            onClick={() => navigate("/library")}
          >
            <Label>Library</Label>
            <span className={styles.navCount}>
              {rows.length}
              <Icon name="chevron" size={10} className={styles.navChevron} />
            </span>
          </button>

          <div className={styles.docs}>
            {rows.map((row) => (
              <Link
                key={row.document_id}
                to={`/documents/${row.document_id}/analysis`}
                className={`${styles.docRow} ${
                  row.document_id === documentId ? styles.docSelected : ""
                }`}
              >
                <span className={styles.docName}>{row.filename}</span>
                <span className={styles.docSub}>
                  {row.pages ?? "—"} pages · {lastAnalysisWords(row.last_analysis).short}
                </span>
              </Link>
            ))}
          </div>
        </div>

        <div className={styles.foot}>
          <div className={styles.deployment}>
            <span>embeddings</span>
            <span className={styles.deploymentValue}>{health.data?.embedder ?? "—"}</span>
          </div>
          <div className={styles.deployment}>
            <span>answer model</span>
            <span className={styles.deploymentValue}>{health.data?.answer_model ?? "—"}</span>
          </div>
          <div className={styles.deployment}>
            <span>analysis model</span>
            <span className={styles.deploymentValue}>{health.data?.analysis_model ?? "—"}</span>
          </div>
        </div>
      </nav>

      <main className={styles.main}>
        <Outlet />
      </main>
    </div>
  );
}

/** The scope, read from the URL. `useMatch` rather than `useParams` because
 *  this component is the layout and sits *above* the route that declares the
 *  parameter. */
export function useActiveDocumentId(): number | null {
  const match = useMatch("/documents/:id/*");
  const raw = match?.params.id;
  if (!raw) return null;
  const id = Number(raw);
  return Number.isInteger(id) && id > 0 ? id : null;
}
