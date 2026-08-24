import { Link } from "react-router-dom";
import styles from "./ModeToggle.module.css";

interface Props {
  /** Where the App half returns to -- the last app page the user was on, so
   *  a trip to the dashboard and back does not lose their document. */
  appPath: string;
  /** Which half is lit. Passed in rather than derived from the link targets:
   *  the App half covers four routes, so "does my `to` match the URL" is the
   *  wrong question -- `/upload` is App mode even when `appPath` is
   *  `/library`. */
  mode: "app" | "kpi";
}

/**
 * App or KPI: the one piece of navigation above the document scope.
 *
 * The dashboard spans every document, so it cannot sit inside the per-document
 * navigation, and it is not a third sidebar entry either -- it is the other
 * half of the workspace. In KPI mode the sidebar drops the document blocks
 * entirely, because no document is in scope.
 */
export function ModeToggle({ appPath, mode }: Props) {
  return (
    <div className={styles.track} role="group" aria-label="Workspace">
      <Half to={appPath} active={mode === "app"}>
        App
      </Half>
      <Half to="/metrics" active={mode === "kpi"}>
        KPI
      </Half>
    </div>
  );
}

function Half({ to, active, children }: { to: string; active: boolean; children: string }) {
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
