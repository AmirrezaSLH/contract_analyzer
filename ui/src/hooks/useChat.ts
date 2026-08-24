import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, BASE, toApiError, type CitationOut, type RetrievalMode } from "../api/client";
import { newTrace } from "../api/client";
import { readFrames, toChatEvent, type DoneEvent, type ToolCallEvent } from "../api/sse";

export interface Turn {
  id: string;
  question: string;
  /** Accumulated `text` deltas. Kept even when a turn fails: an error never
   *  destroys work. */
  answer: string;
  citations: CitationOut[];
  tools: ToolCallEvent[];
  done: DoneEvent | null;
  /** Seconds from sending the question to the `done` event. Measured here
   *  because it is a *client* fact: the API reports what the answer cost and
   *  which model produced it, not how long the user waited for it. */
  elapsedS: number | null;
  /** The stream is open and text is arriving. */
  streaming: boolean;
  /** The stream is open and nothing has arrived yet -- the retrieval trail. */
  working: boolean;
  /** The stream ended without a `done`. A *rendered state*, never a spinner
   *  left running. */
  incomplete: boolean;
  error: ApiError | null;
  traceId: string;
}

export interface AskOptions {
  documentId: number;
  model: string;
  retrievalMode: RetrievalMode;
  topK: number;
}

/** The transcript is capped in memory. `chat()` replays the last eight
 *  messages regardless; this is about not growing a tab without bound. */
const MAX_TURNS = 50;

/**
 * The chat stream: one `AbortController` per turn, no reconnection, and an
 * incomplete turn is something you can see.
 *
 * There is deliberately no retry-on-drop. A stream that silently resumed would
 * replay half an answer; a dropped one is a failed turn that keeps its partial
 * text and offers a retry. The one failure a live demo cannot survive is a
 * spinner that never resolves, so `working` always ends in `streaming`,
 * `incomplete` or `error`.
 */
export function useChat() {
  const [turns, setTurns] = useState<Turn[]>([]);
  const abort = useRef<AbortController | null>(null);

  // Aborted on unmount, or a backgrounded stream keeps appending into a
  // component that is gone.
  useEffect(() => () => abort.current?.abort(), []);

  const update = useCallback((id: string, patch: Partial<Turn> | ((turn: Turn) => Partial<Turn>)) => {
    setTurns((current) =>
      current.map((turn) =>
        turn.id === id ? { ...turn, ...(typeof patch === "function" ? patch(turn) : patch) } : turn,
      ),
    );
  }, []);

  const ask = useCallback(
    async (question: string, options: AskOptions) => {
      // A new question ends the previous stream rather than racing it.
      abort.current?.abort();
      const controller = new AbortController();
      abort.current = controller;

      const traceId = newTrace();
      const id = `${Date.now()}-${traceId}`;
      const history = historyFor(turns);
      const startedAt = performance.now();

      setTurns((current) =>
        [
          ...current,
          {
            id,
            question,
            answer: "",
            citations: [],
            tools: [],
            done: null,
            elapsedS: null,
            streaming: false,
            working: true,
            incomplete: false,
            error: null,
            traceId,
          },
        ].slice(-MAX_TURNS),
      );

      try {
        const response = await fetch(`${BASE}/chat`, {
          method: "POST",
          signal: controller.signal,
          headers: { "Content-Type": "application/json", "X-Trace-Id": traceId },
          body: JSON.stringify({
            document_id: options.documentId,
            question,
            history,
            stream: true,
            model: options.model,
            retrieval_mode: options.retrievalMode,
            top_k: options.topK,
          }),
        });
        // A 503 arrives here, before any body: the API validates the document,
        // the key and the model before it starts streaming, so a 404 is a 404
        // and not an `error` event inside a 200.
        if (!response.ok) throw await toApiError(response);
        if (!response.body) throw new ApiError(0, "unreachable", "The answer stream was empty.");

        let sawDone = false;
        for await (const frame of readFrames(response.body)) {
          const event = toChatEvent(frame);
          if (!event) continue;
          if (event.type === "text") {
            update(id, (turn) => ({
              answer: turn.answer + event.text,
              working: false,
              streaming: true,
            }));
          } else if (event.type === "tool_call") {
            update(id, (turn) => ({ tools: [...turn.tools, event.call] }));
          } else if (event.type === "citations") {
            const answer = event.answer as { citations?: CitationOut[] };
            update(id, { citations: answer.citations ?? [] });
          } else if (event.type === "done") {
            sawDone = true;
            update(id, {
              done: event.done,
              elapsedS: (performance.now() - startedAt) / 1000,
              streaming: false,
              working: false,
            });
          } else {
            sawDone = true;
            update(id, {
              streaming: false,
              working: false,
              error: new ApiError(500, event.code, event.message),
            });
          }
        }
        // The stream ended without a terminal event: the server went away, or
        // the connection dropped. The partial answer stays; the turn says so.
        if (!sawDone) update(id, { streaming: false, working: false, incomplete: true });
      } catch (error) {
        if (controller.signal.aborted) {
          update(id, { streaming: false, working: false, incomplete: true });
          return;
        }
        update(id, {
          streaming: false,
          working: false,
          error:
            error instanceof ApiError
              ? error
              : new ApiError(0, "unreachable", "Could not reach the analyzer."),
        });
      }
    },
    [turns, update],
  );

  const reset = useCallback(() => {
    abort.current?.abort();
    setTurns([]);
  }, []);

  return { turns, ask, reset };
}

/** What the API replays. It caps at eight messages itself; this sends the
 *  turns that actually produced an answer, so a failed turn does not become
 *  half a conversation the model has to make sense of. */
function historyFor(turns: Turn[]): { role: "user" | "assistant"; content: string }[] {
  return turns
    .filter((turn) => turn.answer && !turn.error)
    .flatMap((turn) => [
      { role: "user" as const, content: turn.question },
      { role: "assistant" as const, content: turn.answer },
    ]);
}
