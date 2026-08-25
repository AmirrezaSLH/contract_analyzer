import { Link } from "react-router-dom";
import styles from "./ModeToggle.module.css";

interface Props {
  /** Where the App half returns to -- the last app page the user was on, so
   *  a trip to the dashboard or the log and back does not lose their document. */
  appPath: string;
  /** Which option is lit. Passed in rather than derived from the link targets:
   *  the App half covers four routes, so "does my `to` match the URL" is the
   *  wrong question -- `/upload` is App mode even when `appPath` is
   *  `/library`. */
  mode: "app" | "kpi" | "monitor" | "log";
}

/**
 * App, KPI, Monitor or Log: the navigation above the document scope.
 *
 * KPI, Monitor and Log span every document, so they cannot sit inside the
 * per-document navigation. In those modes the sidebar drops the document
 * blocks, because no document is in scope.
 */
export function ModeToggle({ appPath, mode }: Props) {
  return (
    <div className={styles.track} role="group" aria-label="Workspace">
      <Third to={appPath} active={mode === "app"}>
        App
      </Third>
      <Third to="/metrics" active={mode === "kpi"}>
        KPI
      </Third>
      <Third to="/monitor" active={mode === "monitor"}>
        Monitor
      </Third>
      <Third to="/logs" active={mode === "log"}>
        Log
      </Third>
    </div>
  );
}

function Third({ to, active, children }: { to: string; active: boolean; children: string }) {
  return (
    <Link
      to={to}
      aria-current={active ? "page" : undefined}
      className={`${styles.half} ${active ? styles.active : ""}`}
    >
      {children}
    </Link>
  );
}
