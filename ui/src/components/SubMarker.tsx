import type { SubRequirementStatus } from "../api/client";
import styles from "./SubMarker.module.css";

const WORDS: Record<SubRequirementStatus, string> = {
  met: "met",
  partial: "partially met",
  missing: "missing",
  not_determined: "could not be determined",
};

/** The marker is decoration for a sighted reader and a word for everyone else:
 *  a shape alone tells a screen reader nothing. */
export function SubMarker({ status }: { status: SubRequirementStatus }) {
  return (
    <span className={`${styles.marker} ${styles[status] ?? styles.not_determined}`} role="img"
      aria-label={WORDS[status] ?? WORDS.not_determined} />
  );
}
