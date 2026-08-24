import type { ReactNode } from "react";
import { Card } from "./Card";
import { Label } from "./Label";
import styles from "./MetricTile.module.css";

/**
 * All four done-state tiles are this one component.
 *
 * The value is a slot rather than a string, which is what lets Overall put a
 * `StateChip` in the same place the other three put a number -- and is the
 * reason the four align. The previous front end had one hand-built tile beside
 * three `st.metric` calls, which cannot align and are not cards.
 */
export function MetricTile({ label, value }: { label: string; value: ReactNode }) {
  return (
    <Card className={styles.tile}>
      <Label>{label}</Label>
      {typeof value === "string" || typeof value === "number" ? (
        <span className={styles.value}>{value}</span>
      ) : (
        value
      )}
    </Card>
  );
}
