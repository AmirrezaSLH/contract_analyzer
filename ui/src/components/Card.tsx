import type { HTMLAttributes, ReactNode } from "react";
import styles from "./Card.module.css";

interface Props extends HTMLAttributes<HTMLDivElement> {
  /** 8px rather than 6px: the large containers -- the drop zone, the library
   *  table, the analysis run card. */
  large?: boolean;
  selected?: boolean;
  children: ReactNode;
}

export function Card({ large, selected, className, children, ...rest }: Props) {
  return (
    <div
      {...rest}
      className={[styles.card, large ? styles.lg : "", selected ? styles.selected : "", className ?? ""]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </div>
  );
}
