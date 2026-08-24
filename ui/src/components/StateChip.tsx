import type { ComplianceState } from "../api/client";
import styles from "./StateChip.module.css";

export type ChipState = ComplianceState | "neutral";

interface Props {
  state: ChipState;
  /** Different words, same colour -- the library's "2 gaps found" against the
   *  report's "Partially Compliant". The state still chooses the palette. */
  label?: string;
}

/**
 * The three compliance states and the neutral one, as a chip.
 *
 * **The words are always in it.** A chip with its text removed is a bug, not a
 * compact variant: this is the most important thing on the screen and it must
 * survive being read in greyscale, by someone who cannot distinguish the three
 * hues, or printed.
 *
 * The colour is looked up from a table keyed by the state, so a value the API
 * invents cannot become CSS -- it falls to neutral and still says the words.
 */
export function StateChip({ state, label }: Props) {
  return <span className={`${styles.chip} ${CLASS[state] ?? styles.neutral}`}>{label ?? state}</span>;
}

const CLASS: Record<string, string | undefined> = {
  "Fully Compliant": styles.fully,
  "Partially Compliant": styles.partially,
  "Non-Compliant": styles.non,
  neutral: styles.neutral,
};
