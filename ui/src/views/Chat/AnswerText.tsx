import { Fragment, type ReactNode } from "react";
import styles from "./ChatView.module.css";

/**
 * The answer, with the little markdown the model actually emits.
 *
 * The prompt does not ask for markdown and the spec does not mention it, but
 * the model writes `**Section 6.2 (MFA), p.4:**` anyway, and rendering the
 * asterisks literally is worse than either alternative. So: **bold**, and
 * paragraph breaks on a blank line. Nothing else.
 *
 * Deliberately not a markdown library. This is presentation of two constructs,
 * not a document format, and a parser here would be a dependency with an
 * attack surface for the sake of text this product does not ask for. If the
 * answers ever need lists or tables, the answer is to change the prompt, not
 * to grow this file.
 */
export function AnswerText({ text, caret }: { text: string; caret?: boolean }) {
  const paragraphs = text.split(/\n{2,}/);
  return (
    <div className={styles.answer} aria-live="polite">
      {paragraphs.map((paragraph, index) => (
        <p key={index} className={styles.text}>
          {bold(paragraph)}
          {caret && index === paragraphs.length - 1 ? <span className={styles.caret} /> : null}
        </p>
      ))}
    </div>
  );
}

/** `**…**` to <strong>. Split on the delimiter rather than matched with a
 *  regex over the whole string, so an unclosed `**` mid-stream renders as
 *  literal text instead of swallowing the rest of the answer. */
function bold(text: string): ReactNode[] {
  const parts = text.split("**");
  return parts.map((part, index) =>
    // Odd indexes are between a pair of delimiters -- unless this is the last
    // part, in which case the opening `**` was never closed.
    index % 2 === 1 && index < parts.length - 1 ? (
      <strong key={index}>{part}</strong>
    ) : (
      <Fragment key={index}>{index % 2 === 1 ? `**${part}` : part}</Fragment>
    ),
  );
}
