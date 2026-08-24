import { ErrorSurface } from "../../components/ErrorSurface";
import { PageHead } from "../../components/PageHead";
import { useLogs } from "../../hooks/useLogs";
import styles from "./LogsView.module.css";

/**
 * The live console. Application-level, like `/metrics`: no document scope.
 *
 * Lines are the same compact form stderr prints, with the `api` / `mcp`
 * prefix `start.bash` uses in the terminal.
 */
export function LogsView() {
  const { lines, error, live, scroller } = useLogs();

  return (
    <div className={styles.view}>
      <PageHead
        title="Log"
        subtitle="Live console · api and mcp, as in the terminal"
        actions={
          <span className={styles.status}>{live ? "live" : error ? "disconnected" : "connecting"}</span>
        }
      />

      {error ? <ErrorSurface error={error} as="inline" /> : null}

      <pre ref={scroller} className={styles.console} aria-live="polite">
        {lines.length === 0 && !error ? (
          <span className={styles.empty}>Waiting for log lines…</span>
        ) : (
          lines.map((line, index) => (
            <span key={index} className={styles.row}>
              <span className={line.source === "mcp" ? styles.mcp : styles.api}>{line.source}</span>
              <span className={styles.sep}>│</span>
              <span className={tone(line.level)}>{line.text}</span>
              {"\n"}
            </span>
          ))
        )}
      </pre>
    </div>
  );
}

function tone(level: string): string | undefined {
  if (level === "ERROR" || level === "CRITICAL") return styles.error;
  if (level === "WARNING") return styles.warn;
  return undefined;
}
