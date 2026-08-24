import styles from "./ProgressBar.module.css";

interface Props {
  /** 0 to 1. Omit for the indeterminate variant. */
  value?: number;
  thin?: boolean;
  label?: string;
}

export function ProgressBar({ value, thin, label }: Props) {
  const determinate = value !== undefined;
  return (
    <div
      className={`${styles.track} ${thin ? styles.thin : ""}`}
      role="progressbar"
      aria-label={label}
      aria-valuemin={determinate ? 0 : undefined}
      aria-valuemax={determinate ? 100 : undefined}
      aria-valuenow={determinate ? Math.round(value * 100) : undefined}
    >
      {determinate ? (
        <div className={styles.fill} style={{ width: `${Math.min(100, Math.max(0, value * 100))}%` }} />
      ) : (
        <div className={styles.slider} />
      )}
    </div>
  );
}
