import { useEffect, useId, useRef, useState } from "react";
import { Icon } from "./Icon";
import { Label } from "./Label";
import { Tooltip } from "./Tooltip";
import styles from "./Select.module.css";

interface Props<T extends string> {
  label: string;
  value: T;
  options: readonly T[];
  onChange: (value: T) => void;
  /** Rendered as a tooltip on the label. Retrieval and Depth carry one; Model
   *  does not, because its options are self-describing. */
  help?: string;
  width?: string;
}

/**
 * A listbox, hand-built.
 *
 * Enter or Space opens, arrows move, Enter commits, Escape closes and restores
 * focus to the trigger, click-outside closes. That is the whole reason this is
 * not a `<div>` with an `onClick`: three of these sit in the chat settings row
 * and every one of them has to be operable without a mouse.
 *
 * This is the component most likely to justify adopting a headless primitive.
 * The hand-built one ships; swap it if the keyboard behaviour fights back.
 */
export function Select<T extends string>({ label, value, options, onChange, help, width }: Props<T>) {
  const [open, setOpen] = useState(false);
  const [cursor, setCursor] = useState(0);
  const wrap = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const listId = useId();
  const labelId = useId();

  useEffect(() => {
    if (!open) return;
    setCursor(Math.max(0, options.indexOf(value)));
    const onDown = (event: MouseEvent) => {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [open, options, value]);

  function commit(option: T) {
    onChange(option);
    setOpen(false);
    trigger.current?.focus();
  }

  function onKeyDown(event: React.KeyboardEvent) {
    if (!open) {
      if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
        event.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (event.key === "Escape") {
      event.preventDefault();
      setOpen(false);
      trigger.current?.focus();
    } else if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => (c + 1) % options.length);
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => (c - 1 + options.length) % options.length);
    } else if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      const option = options[cursor];
      if (option !== undefined) commit(option);
    }
  }

  return (
    <div ref={wrap} className={styles.wrap} style={width ? { width } : undefined}>
      <span className={styles.labelRow} id={labelId}>
        {help ? (
          <Tooltip content={help}>
            <Label>{label}</Label>
            <span className={styles.help}>
              <Icon name="info" size={12} weight={2} />
            </span>
          </Tooltip>
        ) : (
          <Label>{label}</Label>
        )}
      </span>

      <button
        ref={trigger}
        type="button"
        className={styles.trigger}
        role="combobox"
        aria-expanded={open}
        aria-controls={listId}
        aria-haspopup="listbox"
        aria-labelledby={labelId}
        onClick={() => setOpen((value) => !value)}
        onKeyDown={onKeyDown}
      >
        <span className={styles.value}>{value}</span>
        <Icon
          name="chevron"
          size={10}
          weight={1.5}
          className={`${styles.chevron} ${open ? styles.chevronOpen : ""}`}
        />
      </button>

      {open ? (
        <div className={styles.menu} id={listId} role="listbox" aria-labelledby={labelId}>
          {options.map((option, index) => (
            <button
              key={option}
              type="button"
              role="option"
              aria-selected={option === value}
              className={`${styles.option} ${option === value ? styles.picked : ""} ${
                index === cursor ? styles.active : ""
              }`}
              onMouseEnter={() => setCursor(index)}
              onClick={() => commit(option)}
            >
              {option}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}
