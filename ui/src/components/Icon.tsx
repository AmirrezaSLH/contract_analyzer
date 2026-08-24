/**
 * The eight icons, and there are only eight.
 *
 * Stroke-only, 1.5-2.0 weight on a 24px grid, `currentColor` so a caller sets
 * the colour by setting text colour. No emoji and no icon font. Adding a ninth
 * is a design decision, not an implementation one.
 */

export type IconName =
  | "upload"
  | "chevron"
  | "document-check"
  | "person"
  | "trash"
  | "info"
  | "document-lines"
  | "send";

interface Props {
  name: IconName;
  size?: number;
  /** Stroke weight. The grid is 24px but these render at 10-34px, and a hair
   *  more weight is what keeps the small ones from disappearing. */
  weight?: number;
  className?: string;
}

export function Icon({ name, size = 16, weight, className }: Props) {
  const path = PATHS[name];
  return (
    <svg
      className={className}
      width={size}
      height={size}
      viewBox={path.box}
      fill="none"
      stroke="currentColor"
      strokeWidth={weight ?? path.weight}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {path.draw.map((d) => (
        <path key={d} d={d} />
      ))}
      {path.circles?.map((c) => (
        <circle key={`${c.cx}-${c.cy}-${c.r}`} cx={c.cx} cy={c.cy} r={c.r} />
      ))}
    </svg>
  );
}

interface Glyph {
  box: string;
  weight: number;
  draw: string[];
  circles?: { cx: number; cy: number; r: number }[];
}

const PATHS: Record<IconName, Glyph> = {
  upload: {
    box: "0 0 24 24",
    weight: 1.5,
    draw: ["M12 15V3", "M8 7l4-4 4 4", "M3 15v3a3 3 0 0 0 3 3h12a3 3 0 0 0 3-3v-3"],
  },
  // One chevron, rotated by whoever uses it -- never redrawn per direction.
  // It points right at rest, which is the disclosure's closed state.
  chevron: { box: "0 0 10 10", weight: 1.6, draw: ["M3.5 1.5 L7 5 L3.5 8.5"] },
  "document-check": {
    box: "0 0 24 24",
    weight: 1.8,
    draw: ["M6 3h8l4 4v14H6z", "M14 3v4h4", "M9 13l2 2 4-4"],
  },
  person: {
    box: "0 0 24 24",
    weight: 1.8,
    draw: ["M5 20c0-3.6 3.1-6 7-6s7 2.4 7 6"],
    circles: [{ cx: 12, cy: 8, r: 3.5 }],
  },
  trash: { box: "0 0 24 24", weight: 1.8, draw: ["M4 6h16", "M9 6V4h6v2", "M6 6l1 14h10l1-14"] },
  info: {
    box: "0 0 24 24",
    weight: 2,
    draw: ["M12 11.2v5", "M12 7.4v0.2"],
    circles: [{ cx: 12, cy: 12, r: 9 }],
  },
  "document-lines": {
    box: "0 0 24 24",
    weight: 1.5,
    draw: ["M6 3h8l4 4v14H6z", "M14 3v4h4", "M9 14h6", "M9 17.5h4"],
  },
  send: { box: "0 0 24 24", weight: 1.9, draw: ["M5 12h13", "M12 5l7 7-7 7"] },
};
