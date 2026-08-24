import { useEffect, useRef, useState } from "react";

import { ApiError, BASE, toApiError } from "../api/client";
import { readFrames } from "../api/sse";

export type LogSource = "api" | "mcp";

export interface LogLine {
  text: string;
  level: string;
  source: LogSource;
}

/** A forgotten tab must not grow without bound. Newest kept. */
const MAX_LINES = 1000;

/**
 * The live console: one GET stream, abort on unmount, cap the buffer.
 *
 * Reuses the chat reader's framing. This stream is GET and never sends a
 * terminal event, so ending it is the browser leaving the page.
 */
export function useLogs() {
  const [lines, setLines] = useState<LogLine[]>([]);
  const [error, setError] = useState<ApiError | null>(null);
  const [live, setLive] = useState(false);
  const scroller = useRef<HTMLPreElement>(null);

  useEffect(() => {
    const controller = new AbortController();
    let cancelled = false;

    (async () => {
      try {
        const response = await fetch(`${BASE}/logs/events`, { signal: controller.signal });
        if (!response.ok) throw await toApiError(response);
        if (!response.body) {
          throw new ApiError(0, "unreachable", "The log stream was empty.");
        }
        setLines([]);
        setLive(true);
        setError(null);
        for await (const frame of readFrames(response.body)) {
          if (frame.event !== "log") continue;
          let payload: { line?: string; level?: string; source?: string };
          try {
            payload = JSON.parse(frame.data) as {
              line?: string;
              level?: string;
              source?: string;
            };
          } catch {
            continue;
          }
          const text = String(payload.line ?? "");
          if (!text) continue;
          const level = String(payload.level ?? "INFO");
          const source: LogSource = payload.source === "mcp" ? "mcp" : "api";
          setLines((current) => [...current, { text, level, source }].slice(-MAX_LINES));
        }
        if (!cancelled) setLive(false);
      } catch (caught) {
        if (controller.signal.aborted) return;
        setLive(false);
        setError(
          caught instanceof ApiError
            ? caught
            : new ApiError(0, "unreachable", "Could not reach the analyzer."),
        );
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, []);

  useEffect(() => {
    const node = scroller.current;
    if (node) node.scrollTop = node.scrollHeight;
  }, [lines]);

  return { lines, error, live, scroller };
}
