import type { ReactNode } from "react";
import styles from "./Banner.module.css";

interface Props {
  tone: "error" | "warn" | "info";
  title: string;
  /** The API's own message, when it says something the title does not. */
  body?: string;
  /** Always the second line. */
  hint?: string;
  traceId?: string;
  action?: ReactNode;
}

export function Banner({ tone, title, body, hint, traceId, action }: Props) {
  return (
    <div className={`${styles.banner} ${styles[tone]}`} role={tone === "error" ? "alert" : "status"}>
      <span className={styles.title}>{title}</span>
      {body ? <span className={styles.hint}>{body}</span> : null}
      {hint ? <span className={styles.hint}>{hint}</span> : null}
      {traceId ? <span className={styles.trace}>trace {traceId}</span> : null}
      {action ? <div className={styles.actions}>{action}</div> : null}
    </div>
  );
}
