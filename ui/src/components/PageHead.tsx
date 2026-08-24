import type { ReactNode } from "react";
import styles from "../App.module.css";

interface Props {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
}

/** The page title and its meta line, in the serif at 30px/600. Rendered by
 *  each view rather than derived by the shell: the analysis head changes with
 *  the run, and a shell that computed it would need the run's state. */
export function PageHead({ title, subtitle, actions }: Props) {
  return (
    <header className={styles.head}>
      <div className={styles.headText}>
        <h1 className={styles.title}>{title}</h1>
        {subtitle ? <span className={styles.subtitle}>{subtitle}</span> : null}
      </div>
      {actions ? <div className={styles.headActions}>{actions}</div> : null}
    </header>
  );
}
