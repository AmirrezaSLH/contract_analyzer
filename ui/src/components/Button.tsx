import type { ButtonHTMLAttributes, ReactNode } from "react";
import styles from "./Button.module.css";
import { Tooltip } from "./Tooltip";

type Variant = "primary" | "secondary" | "tertiary" | "destructive" | "icon";
type Size = "lg" | "md" | "sm";

interface Props extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "children"> {
  variant?: Variant;
  size?: Size;
  /** Why the button is disabled, shown on hover and on focus.
   *
   *  **A disabled control must always say why.** A greyed-out "Run compliance
   *  analysis" with no explanation is the difference between "this product is
   *  broken" and "set ANTHROPIC_API_KEY". */
  disabledReason?: string;
  block?: boolean;
  children: ReactNode;
}

export function Button({
  variant = "secondary",
  size = "md",
  disabledReason,
  block,
  className,
  children,
  ...rest
}: Props) {
  const button = (
    <button
      type="button"
      {...rest}
      disabled={rest.disabled || Boolean(disabledReason)}
      className={[
        styles.button,
        styles[variant],
        styles[size],
        block ? styles.block : "",
        className ?? "",
      ]
        .filter(Boolean)
        .join(" ")}
    >
      {children}
    </button>
  );
  if (!disabledReason) return button;
  // A disabled button fires no pointer events, so the tooltip has to listen on
  // a wrapper. Focusable, so the reason is reachable without a mouse.
  return <Tooltip content={disabledReason}>{button}</Tooltip>;
}
