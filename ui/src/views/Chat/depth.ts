/**
 * Depth is an abstraction over `retrieval_top_k`, and the number never reaches
 * the screen.
 *
 * This is the one place the UI knowingly hides a parameter, and it is worth
 * saying why: a compliance reviewer has no basis for choosing 4 passages over
 * 8, but does have a basis for choosing "deep" when a clause is buried in an
 * exhibit. The mapping is the front end's; the API only ever sees a number.
 *
 * **`medium` is `settings.retrieval_top_k`, by construction.** The plan wrote
 * the mapping as a hardcoded `{shallow: 3, medium: 6, deep: 12}` *and* required
 * medium to equal the configured default -- two statements that only agree
 * while `retrieval_top_k` happens to be 6. Deriving it from `/health` makes the
 * invariant hold whatever the deployment is tuned to, and the placeholder is
 * then a *ratio* rather than a number.
 *
 * **The ratios are a labelled placeholder.** Half and double are a guess. They
 * need a recall measurement against the five criteria, which is one script and
 * is what would make this control defensible when someone asks about it.
 */

export const DEPTHS = ["shallow", "medium", "deep"] as const;
export type Depth = (typeof DEPTHS)[number];

/** `POST /chat` clamps `top_k` to 1..20 and rejects anything outside it, so the
 *  mapping clamps too rather than sending a value that would 422. */
const MIN = 1;
const MAX = 20;

export function topKFor(depth: Depth, configured: number): number {
  const medium = clamp(Math.round(configured));
  switch (depth) {
    case "shallow":
      return clamp(Math.round(medium / 2));
    case "deep":
      return clamp(medium * 2);
    case "medium":
      return medium;
  }
}

function clamp(value: number): number {
  if (!Number.isFinite(value)) return 6;
  return Math.min(MAX, Math.max(MIN, value));
}
