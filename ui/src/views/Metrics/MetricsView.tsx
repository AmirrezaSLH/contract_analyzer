import { EmptyState } from "../../components/EmptyState";
import { PageHead } from "../../components/PageHead";
import styles from "./MetricsView.module.css";

/**
 * The KPI dashboard.
 *
 * The design is settled -- five operational tiles, three quality meters with
 * their thresholds, two trend charts and a runs table, all of it fed by
 * `/metrics/summary` and `/metrics/timeseries` (see `KPI_01/00_README.md`).
 * This is the route and the shell for it; the bands land next.
 */
export function MetricsView() {
  return (
    <div className={styles.view}>
      <PageHead title="Operations" subtitle="Across every document in this deployment" />
      <EmptyState
        title="The dashboard is not built yet"
        body="Five operational tiles, three quality meters with their thresholds, runs and latency over the window, and the recent runs table. Every figure comes from the metrics store that already records them."
      />
    </div>
  );
}
