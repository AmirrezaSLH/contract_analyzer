import type { ReactNode } from "react";
import { Card } from "./Card";
import { Label } from "./Label";
import styles from "./MetricTile.module.css";

interface Props {
  label: string;
  value: ReactNode;
  /** The line under the number. On the KPI page it carries the **threshold**,
   *  so the value and the bar it has to clear are never separated, and the
   *  denominator, so a rate can be told apart from an unmeasured one. */
  sub?: ReactNode;
  /** A status chip, in the label row. **It carries words**; colour never says
   *  anything on its own here. */
  chip?: ReactNode;
}

/**
 * All four done-state tiles and all five KPI tiles are this one component.
 *
 * The value is a slot rather than a string, which is what lets Overall put a
 * `StateChip` in the same place the other three put a number -- and is the
 * reason the four align. The previous front end had one hand-built tile beside
 * three `st.metric` calls, which cannot align and are not cards.
 */
export function MetricTile({ label, value, sub, chip }: Props) {
  return (
    <Card className={styles.tile}>
      <div className={styles.head}>
        <Label>{label}</Label>
        {chip}
      </div>
      {typeof value === "string" || typeof value === "number" ? (
        <span className={styles.value}>{value}</span>
      ) : (
        value
      )}
      {sub ? <span className={styles.sub}>{sub}</span> : null}
    </Card>
  );
}
