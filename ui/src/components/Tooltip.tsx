import { useEffect, useId, useRef, useState, type ReactNode } from "react";
import styles from "./Tooltip.module.css";

interface Props {
  content: string;
  children: ReactNode;
}

/**
 * Hover **and** focus, and Escape dismisses.
 *
 * The focus half is not a nicety: `Select`'s help mark and every
 * `disabledReason` put load-bearing text in here, and a tooltip that only
 * answers to a mouse hides it from everyone else.
 */
export function Tooltip({ content, children }: Props) {
  const [open, setOpen] = useState(false);
  const id = useId();
  const wrap = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  return (
    <span
      ref={wrap}
      className={styles.wrap}
      tabIndex={0}
      aria-describedby={open ? id : undefined}
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
      onFocus={() => setOpen(true)}
      onBlur={() => setOpen(false)}
    >
      {children}
      <span id={id} role="tooltip" className={`${styles.tip} ${open ? styles.open : ""}`}>
        {content}
      </span>
    </span>
  );
}
