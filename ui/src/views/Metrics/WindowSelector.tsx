import type { MetricsWindow } from "../../api/client";
import styles from "./MetricsView.module.css";

/** The three windows, and the bucket the server pairs with each. The pairing
 *  itself lives in `windows.DEFAULT_BUCKETS`; these are the words for it. */
export const WINDOWS: { value: MetricsWindow; label: string; bucket: string }[] = [
  { value: "24h", label: "24 hours", bucket: "hourly buckets" },
  { value: "7d", label: "7 days", bucket: "6-hour buckets" },
  { value: "30d", label: "30 days", bucket: "daily buckets" },
];

interface Props {
  value: MetricsWindow;
  onChange: (window: MetricsWindow) => void;
}

/**
 * One control, three options, and it drives **both** query parameters.
 *
 * The page sends only `window`; the server chooses the bucket. That pairing
 * lives in one place so the API and the design cannot drift, and because
 * thirty days of one-hour bars is 720 marks on a 900-pixel axis.
 *
 * The selection is `useState` and not the URL: it is a view preference, not
 * scope. (`?window=` is the change if a shareable dashboard link is ever
 * wanted, and it is a small one.)
 *
 * Buttons in a `role="group"`, not a tablist -- there is no tab panel here,
 * and the same reason `ModeToggle` is two links. The look is shared with it
 * through `Segmented.module.css`.
 */
export function WindowSelector({ value, onChange }: Props) {
  return (
    <div className={styles.windows} role="group" aria-label="Window">
      {WINDOWS.map((window) => (
        <button
          key={window.value}
          type="button"
          aria-pressed={window.value === value}
          className={`${styles.window} ${window.value === value ? styles.windowActive : ""}`}
          onClick={() => onChange(window.value)}
        >
          {window.label}
        </button>
      ))}
    </div>
  );
}

export function bucketWords(window: MetricsWindow): string {
  return WINDOWS.find((entry) => entry.value === window)?.bucket ?? "";
}
