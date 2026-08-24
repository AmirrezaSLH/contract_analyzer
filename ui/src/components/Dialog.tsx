import { useEffect, useId, useRef, type ReactNode } from "react";
import styles from "./Dialog.module.css";

interface Props {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  actions: ReactNode;
}

/**
 * A modal with a focus trap, Escape, and focus restored on close.
 *
 * The trap is the part that has to be written rather than wished for: without
 * it, Tab walks straight out of a confirmation dialog and into the page behind
 * it, and the next Enter presses whatever it landed on.
 */
export function Dialog({ open, onClose, title, children, actions }: Props) {
  const panel = useRef<HTMLDivElement>(null);
  const restoreTo = useRef<HTMLElement | null>(null);
  const titleId = useId();

  useEffect(() => {
    if (!open) return;
    restoreTo.current = document.activeElement as HTMLElement | null;
    // The first focusable thing in the dialog, so a keyboard user starts
    // inside it rather than wherever they were.
    focusable(panel.current)[0]?.focus();

    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        onClose();
        return;
      }
      if (event.key !== "Tab") return;
      const items = focusable(panel.current);
      if (items.length === 0) return;
      const first = items[0]!;
      const last = items[items.length - 1]!;
      const active = document.activeElement;
      if (event.shiftKey && active === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && active === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKey, true);
    return () => {
      document.removeEventListener("keydown", onKey, true);
      restoreTo.current?.focus();
    };
  }, [open, onClose]);

  if (!open) return null;
  return (
    <div
      className={styles.backdrop}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        className={styles.dialog}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
      >
        <h2 id={titleId} className={styles.title}>
          {title}
        </h2>
        <div className={styles.body}>{children}</div>
        <div className={styles.actions}>{actions}</div>
      </div>
    </div>
  );
}

const FOCUSABLE = 'button:not([disabled]), [href], input, select, textarea, [tabindex]:not([tabindex="-1"])';

function focusable(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE));
}
