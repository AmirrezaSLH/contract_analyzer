/**
 * Units, formatted once, at the edge.
 *
 * `03_data_contract.md` trap 6: `latency_s` is **seconds** and
 * `chat.latency_ms` is **milliseconds**, and they sit in the same payload.
 * Dividing by 1000 at three call sites is how one of them ends up wrong, so
 * the conversion happens here and nowhere else.
 *
 * The other rule these functions carry: **`null` is not zero.** A rate is
 * `null` when its denominator is zero, and a `quote_verification_rate` of
 * `null` with `quotes_total: 0` means no quote was produced -- a different and
 * more alarming fact than 0% verified. Everything here renders `null` as an
 * em dash, never as `0`.
 */

/** What an absent number looks like. Never `0`, never a blank cell. */
export const DASH = "—";

/** Seconds, as `104 s`. Sub-minute precision is what a p95 is read for; a
 *  minute-and-second rendering hides the difference between 61 s and 118 s. */
export function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  if (value < 10) return `${round(value, 1)} s`;
  return `${Math.round(value)} s`;
}

/** Milliseconds -- `chat.latency_ms`, and only that. The one division. */
export function millis(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  return seconds(value / 1000);
}

/** Dollars. Two places up to $10, so a $0.93 per-run cost is not `$1`. */
export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return DASH;
  const places = Math.abs(value) < 10 ? 2 : Math.abs(value) < 1000 ? 2 : 0;
  return `$${value.toFixed(places)}`;
}

/** A rate, as a percentage. `0.0588` -> `5.9%`. */
export function percent(rate: number | null | undefined, places = 1): string {
  if (rate === null || rate === undefined) return DASH;
  return `${round(rate * 100, places)}%`;
}

/** A rate's denominator, always beside it: `306 of 310`. */
export function ratio(part: number, whole: number, noun?: string): string {
  const tail = noun ? ` ${noun}` : "";
  return `${part} of ${whole}${tail}`;
}

/** `3 documents`, `1 document`. */
export function plural(count: number, noun: string, many = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : many}`;
}

/** How long ago, in the words the refreshed line uses. Seconds below a minute
 *  because the summary polls every five: `refreshed 2 m ago` on a five-second
 *  poll would say the page was stale when it was not. */
export function ago(at: number | null | undefined, now = Date.now()): string {
  if (!at) return DASH;
  const elapsed = Math.max(0, Math.round((now - at) / 1000));
  if (elapsed < 60) return `${elapsed} s ago`;
  if (elapsed < 3600) return `${Math.round(elapsed / 60)} m ago`;
  return `${Math.round(elapsed / 3600)} h ago`;
}

/** A run's `created_at`, as a clock time. The runs table is newest-first over
 *  a few days at most, so the date only appears when it is not today. */
export function startedAt(iso: string, now = new Date()): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return DASH;
  const clock = time(at);
  return sameDay(at, now) ? clock : `${day(at)} ${clock}`;
}

/**
 * A bucket label, at the resolution its window is bucketed at.
 *
 * 24 h is one-hour buckets and wants a clock; 7 d is six-hour buckets and
 * needs the day as well, or four bars a day all read `12:00`; 30 d is daily
 * and the clock would be a lie about precision the bucket does not have.
 */
export function bucketLabel(iso: string, window: string, opts: { short?: boolean } = {}): string {
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return iso;
  if (window === "24h" || window === "30m" || window === "1h") return time(at);
  if (window === "30d") return day(at);
  return opts.short ? day(at) : `${day(at)} ${time(at)}`;
}

function time(at: Date): string {
  return `${String(at.getHours()).padStart(2, "0")}:${String(at.getMinutes()).padStart(2, "0")}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

function day(at: Date): string {
  return `${at.getDate()} ${MONTHS[at.getMonth()]}`;
}

function sameDay(a: Date, b: Date): boolean {
  return (
    a.getFullYear() === b.getFullYear() &&
    a.getMonth() === b.getMonth() &&
    a.getDate() === b.getDate()
  );
}

/** Rounds without printing trailing zeros: `98.70%` reads as more precision
 *  than a rate over 310 quotes has. */
function round(value: number, places: number): string {
  return String(Number(value.toFixed(places)));
}
