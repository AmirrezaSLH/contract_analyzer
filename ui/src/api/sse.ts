/**
 * A server-sent event reader, pure and free of React.
 *
 * **`EventSource` cannot be used.** It is GET-only and cannot set headers, and
 * `/api/chat` is a POST carrying a JSON body. So this is `fetch` plus a
 * `ReadableStream` reader and about forty lines of framing.
 *
 * Three things `EventSource` would have given for free and are written here:
 *
 *   1. **Buffer discipline.** SSE frames split across chunk boundaries. This
 *      parses from an accumulating buffer and never per chunk, which is the
 *      single most likely source of a subtle bug in this front end -- and the
 *      reason this module is pure, separate, and tested against fixtures that
 *      split mid-frame.
 *   2. **Cancellation.** An `AbortController` per turn, which the caller owns.
 *      Without it a backgrounded stream keeps appending into a component that
 *      is gone.
 *   3. **Reconnection.** There is none, deliberately. A dropped stream is a
 *      failed turn: the caller keeps the partial text and marks the turn
 *      incomplete. A stream that silently resumes would replay an answer.
 */

/** One frame. `event:` defaults to `message` per the SSE specification. */
export interface SseFrame {
  event: string;
  data: string;
}

/**
 * Split an accumulating buffer into whole frames.
 *
 * Returns the frames that are complete and whatever is left over, which the
 * caller prepends to the next chunk. A frame ends at a blank line; anything
 * after the last blank line is by definition incomplete.
 */
export function parseFrames(buffer: string): { frames: SseFrame[]; rest: string } {
  // Normalised first: a server, a proxy or a test fixture may use any of the
  // three line endings the specification allows, and a frame boundary that
  // only matches "\n\n" would silently never fire on "\r\n\r\n".
  const normalised = buffer.replace(/\r\n/g, "\n").replace(/\r/g, "\n");
  const parts = normalised.split("\n\n");
  const rest = parts.pop() ?? "";
  const frames: SseFrame[] = [];
  for (const part of parts) {
    const frame = parseFrame(part);
    if (frame) frames.push(frame);
  }
  return { frames, rest };
}

function parseFrame(block: string): SseFrame | null {
  let event = "message";
  const data: string[] = [];
  for (const line of block.split("\n")) {
    // A line beginning with a colon is a comment. `sse-starlette`'s keepalive
    // is exactly that, and treating it as data would append ": ping" to an
    // answer every fifteen seconds.
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    // One optional space after the colon is part of the framing, not the data.
    const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
    if (field === "event") event = value;
    else if (field === "data") data.push(value);
  }
  if (data.length === 0 && event === "message") return null;
  // Multiple `data:` lines in one frame are joined with newlines, which is
  // what the specification says and what a multi-line JSON payload needs.
  return { event, data: data.join("\n") };
}

/** Read a response body as frames. Yields nothing and returns cleanly when the
 *  stream ends, whether it ended because the server closed it or because the
 *  caller aborted. A truncated final frame is dropped rather than guessed at. */
export async function* readFrames(body: ReadableStream<Uint8Array>): AsyncGenerator<SseFrame> {
  const reader = body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += value;
      const { frames, rest } = parseFrames(buffer);
      buffer = rest;
      yield* frames;
    }
    // The server may close without a trailing blank line. What is left is a
    // whole frame only if it parses as one; a half-written `data:` is not.
    const { frames } = parseFrames(buffer + "\n\n");
    yield* frames;
  } finally {
    // Releasing the lock rather than cancelling: an aborted request has
    // already torn the body down, and cancelling a dead stream throws.
    reader.releaseLock();
  }
}

// --------------------------------------------------------------------------
// The chat stream, typed
// --------------------------------------------------------------------------

export interface ToolCallEvent {
  name: string;
  args?: Record<string, unknown> | null;
  returned?: number | null;
  new?: number | null;
  error?: string | null;
}

export interface DoneEvent {
  usage: Record<string, number>;
  cost_usd: number;
  model: string;
  stop_reason: string;
  ended_by: string;
  tool_calls: number;
  grounded: boolean;
}

export type ChatEvent =
  | { type: "text"; text: string }
  | { type: "tool_call"; call: ToolCallEvent }
  | { type: "citations"; answer: unknown }
  | { type: "done"; done: DoneEvent }
  | { type: "error"; code: string; message: string };

/** The API's frames as this front end's events. Anything unrecognised is
 *  dropped: the API may grow an event before the UI reads it, and an unknown
 *  frame is not a reason to end a working stream. */
export function toChatEvent(frame: SseFrame): ChatEvent | null {
  let payload: Record<string, unknown>;
  try {
    payload = JSON.parse(frame.data) as Record<string, unknown>;
  } catch {
    return null;
  }
  switch (frame.event) {
    case "text":
      return { type: "text", text: String(payload.text ?? "") };
    case "tool_call":
      return { type: "tool_call", call: payload as unknown as ToolCallEvent };
    case "citations":
      return { type: "citations", answer: payload };
    case "done":
      return { type: "done", done: payload as unknown as DoneEvent };
    case "error":
      return {
        type: "error",
        code: String(payload.code ?? "internal"),
        message: String(payload.message ?? "The answer stream failed."),
      };
    default:
      return null;
  }
}
