import styles from "./QuoteCard.module.css";

/** What both endpoints return. `CitationOut` and `ResolvedQuote` agree on
 *  `text`, `section_ref` and `verified` -- deliberately, so one card renders a
 *  quote from a report and a quote from an answer, and "verified" means the
 *  same thing on both screens. */
export interface Quote {
  text: string;
  section_ref: string;
  page_display?: string;
  verified?: boolean;
}

interface Props {
  quote: Quote;
  tone?: "analysis" | "chat";
}

export function QuoteCard({ quote, tone = "analysis" }: Props) {
  const verified = quote.verified !== false;
  return (
    <figure
      className={`${styles.quote} ${styles[tone]} ${verified ? "" : styles.unverified}`}
    >
      {/* Typographic quotes, and in the serif: this is text *from the
          contract*, and the face is what marks it as such. */}
      <blockquote className={styles.text}>&ldquo;{quote.text}&rdquo;</blockquote>
      <figcaption className={styles.meta}>
        § {quote.section_ref}
        {quote.page_display ? ` · p. ${quote.page_display}` : ""} ·{" "}
        {verified ? "verified" : "not found verbatim — check the source"}
      </figcaption>
    </figure>
  );
}
