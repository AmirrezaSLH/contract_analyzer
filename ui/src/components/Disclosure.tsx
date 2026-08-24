import { useId, type ReactNode } from "react";
import { Card } from "./Card";
import { Icon } from "./Icon";
import styles from "./Disclosure.module.css";

interface Props {
  open: boolean;
  onToggle: () => void;
  /** The full header cluster, not a label. Everything a reader needs in order
   *  to decide whether to open the row must live here and be visible while
   *  collapsed; moving any of it inside is a functional regression. */
  header: ReactNode;
  children: ReactNode;
}

export function Disclosure({ open, onToggle, header, children }: Props) {
  const bodyId = useId();
  return (
    <Card>
      <button
        type="button"
        className={`${styles.head} ${open ? styles.headOpen : ""}`}
        aria-expanded={open}
        aria-controls={bodyId}
        onClick={onToggle}
      >
        <Icon
          name="chevron"
          size={11}
          className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`}
        />
        {header}
      </button>
      {open ? (
        <div id={bodyId} className={styles.body}>
          {children}
        </div>
      ) : null}
    </Card>
  );
}
