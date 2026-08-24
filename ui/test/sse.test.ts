import { describe, expect, it } from "vitest";
import { parseFrames, readFrames, toChatEvent } from "../src/api/sse";

/**
 * The framing, against the case that actually breaks it.
 *
 * SSE frames split across chunk boundaries, and parsing per chunk instead of
 * from an accumulating buffer is the single most likely subtle bug in this
 * front end. Every test here feeds the parser a stream chopped somewhere it
 * would rather not be chopped.
 */

function stream(chunks: string[]): ReadableStream<Uint8Array> {
  const encoder = new TextEncoder();
  return new ReadableStream({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
}

async function collect(chunks: string[]) {
  const frames = [];
  for await (const frame of readFrames(stream(chunks))) frames.push(frame);
  return frames;
}

describe("parseFrames", () => {
  it("returns whole frames and keeps the remainder", () => {
    const { frames, rest } = parseFrames('event: text\ndata: {"a":1}\n\nevent: done\ndata: {');
    expect(frames).toEqual([{ event: "text", data: '{"a":1}' }]);
    expect(rest).toBe('event: done\ndata: {');
  });

  it("defaults the event name to message, per the specification", () => {
    const { frames } = parseFrames("data: hello\n\n");
    expect(frames).toEqual([{ event: "message", data: "hello" }]);
  });

  it("joins multiple data lines with a newline", () => {
    const { frames } = parseFrames("event: text\ndata: one\ndata: two\n\n");
    expect(frames[0]!.data).toBe("one\ntwo");
  });

  it("ignores comment lines", () => {
    // `sse-starlette`'s keepalive is a bare comment every fifteen seconds.
    // Treating it as data would append ': ping' to an answer.
    const { frames } = parseFrames(": ping\n\nevent: text\ndata: hi\n\n");
    expect(frames).toEqual([{ event: "text", data: "hi" }]);
  });

  it("accepts CRLF as well as LF", () => {
    const { frames } = parseFrames("event: text\r\ndata: hi\r\n\r\n");
    expect(frames).toEqual([{ event: "text", data: "hi" }]);
  });

  it("strips exactly one leading space after the colon", () => {
    const { frames } = parseFrames("data:  two spaces\n\n");
    expect(frames[0]!.data).toBe(" two spaces");
  });
});

describe("readFrames", () => {
  it("reassembles a frame split mid-field", async () => {
    const frames = await collect(["event: te", "xt\ndata: {\"text\":\"hel", 'lo"}\n\n']);
    expect(frames).toEqual([{ event: "text", data: '{"text":"hello"}' }]);
  });

  it("reassembles a frame split exactly on the blank-line boundary", async () => {
    const frames = await collect(["event: text\ndata: a\n", "\nevent: text\ndata: b\n\n"]);
    expect(frames.map((f) => f.data)).toEqual(["a", "b"]);
  });

  it("handles several frames arriving in one chunk", async () => {
    const frames = await collect(["event: text\ndata: a\n\nevent: text\ndata: b\n\n"]);
    expect(frames).toHaveLength(2);
  });

  it("does not hang or invent a frame when the stream is truncated mid-field", async () => {
    // The API went away mid-frame. Whatever arrived whole is kept; the partial
    // frame is dropped rather than guessed at.
    //
    // The tail cannot be flushed, because `data: par` is a syntactically
    // *complete* field -- a parser that flushed it could not tell a truncated
    // frame from a whole one, and would invent the difference. Nothing is
    // lost: SSE framing always writes the blank line after an event, so a
    // frame the server finished sending has already been dispatched.
    const frames = await collect(["event: text\ndata: a\n\nevent: text\ndata: par"]);
    expect(frames).toEqual([{ event: "text", data: "a" }]);
  });

  it("dispatches a frame the server terminated properly and then closed on", async () => {
    const frames = await collect(["event: done\ndata: {}\n\n"]);
    expect(frames).toEqual([{ event: "done", data: "{}" }]);
  });

  it("returns cleanly on an empty stream", async () => {
    expect(await collect([])).toEqual([]);
  });
});

describe("toChatEvent", () => {
  it("maps each event the API sends", () => {
    expect(toChatEvent({ event: "text", data: '{"text":"hi"}' })).toEqual({ type: "text", text: "hi" });
    expect(toChatEvent({ event: "done", data: '{"cost_usd":0.1}' })?.type).toBe("done");
    expect(toChatEvent({ event: "citations", data: '{"citations":[]}' })?.type).toBe("citations");
    expect(toChatEvent({ event: "tool_call", data: '{"name":"search"}' })?.type).toBe("tool_call");
  });

  it("terminates cleanly on an error event, carrying the code", () => {
    const event = toChatEvent({ event: "error", data: '{"code":"upstream_failure","message":"no"}' });
    expect(event).toEqual({ type: "error", code: "upstream_failure", message: "no" });
  });

  it("drops an event it does not know rather than ending a working stream", () => {
    // The API may grow an event before this front end reads it. That is not a
    // reason to tear down an answer that is arriving.
    expect(toChatEvent({ event: "heartbeat", data: "{}" })).toBeNull();
  });

  it("drops a frame whose data is not JSON", () => {
    expect(toChatEvent({ event: "text", data: "not json" })).toBeNull();
  });
});
