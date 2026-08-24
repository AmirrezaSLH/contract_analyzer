import type { ComplianceState } from "../api/client";
import styles from "./StateChip.module.css";

/**
 * A compliance state, or one of the three status tones.
 *
 * The KPI page has no compliance states on it -- a threshold is met, missed,
 * or unmeasured -- but it needs exactly the palette this component already
 * owns, and a second chip component would be a second set of colours to keep
 * measured. So the tones are aliases onto the same three classes: `good` is
 * the Fully Compliant green, `warn` the Partially Compliant amber, `neutral`
 * the grey. Nothing else changes, including the rule below.
 */
export type ChipState = ComplianceState | "neutral" | "good" | "warn";

interface Props {
  state: ChipState;
  /** 11px rather than 12px, for the chip that sits inside a tile's label row
   *  beside an 11px `Label`. */
  size?: "sm";
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
export function StateChip({ state, label, size }: Props) {
  return (
    <span
      className={[styles.chip, CLASS[state] ?? styles.neutral, size === "sm" ? styles.sm : ""]
        .filter(Boolean)
        .join(" ")}
    >
      {label ?? state}
    </span>
  );
}

const CLASS: Record<string, string | undefined> = {
  "Fully Compliant": styles.fully,
  "Partially Compliant": styles.partially,
  "Non-Compliant": styles.non,
  neutral: styles.neutral,
  good: styles.fully,
  warn: styles.partially,
};
