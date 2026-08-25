import kpi from "../Metrics/MetricsView.module.css";
import type { MonitorWindow } from "./types";

/** Monitor's windows. KPI also offers these two, plus 24h / 7d / 30d. */
export const WINDOWS: { value: MonitorWindow; label: string; bucket: string }[] = [
  { value: "30m", label: "30 minutes", bucket: "1-minute buckets" },
  { value: "1h", label: "1 hour", bucket: "1-minute buckets" },
];

interface Props {
  value: MonitorWindow;
  onChange: (window: MonitorWindow) => void;
}

export function WindowSelector({ value, onChange }: Props) {
  return (
    <div className={kpi.windows} role="group" aria-label="Window">
      {WINDOWS.map((window) => (
        <button
          key={window.value}
          type="button"
          aria-pressed={window.value === value}
          className={`${kpi.window} ${window.value === value ? kpi.windowActive : ""}`}
          onClick={() => onChange(window.value)}
        >
          {window.label}
        </button>
      ))}
    </div>
  );
}

export function bucketWords(window: MonitorWindow): string {
  return WINDOWS.find((entry) => entry.value === window)?.bucket ?? "";
}
