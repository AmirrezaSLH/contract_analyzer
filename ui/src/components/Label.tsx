import type { ReactNode } from "react";
import styles from "./Label.module.css";

export function Label({ children, className }: { children: ReactNode; className?: string }) {
  return <span className={`${styles.label} ${className ?? ""}`}>{children}</span>;
}
