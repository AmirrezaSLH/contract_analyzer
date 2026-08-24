import { useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { ApiError, type RetrievalMode } from "../../api/client";
import { DocumentTabs } from "../../components/DocumentTabs";
import { ErrorSurface } from "../../components/ErrorSurface";
import { Icon } from "../../components/Icon";
import { NO_KEY_REASON, NoKeyBanner } from "../../components/NoKeyBanner";
import { PageHead } from "../../components/PageHead";
import { ProgressBar } from "../../components/ProgressBar";
import { QuoteCard } from "../../components/QuoteCard";
import { Select } from "../../components/Select";
import { useChat, type Turn } from "../../hooks/useChat";
import { useDocument } from "../../hooks/useDocuments";
import { useHealth } from "../../hooks/useHealth";
import { AnswerText } from "./AnswerText";
import { DEPTHS, topKFor, type Depth } from "./depth";
import styles from "./ChatView.module.css";

const RETRIEVAL_MODES = ["hybrid", "vector", "keyword"] as const;

const RETRIEVAL_HELP =
  "How the contract is searched. Hybrid fuses vector and keyword results — the safest for contract language, where a defined term matters as much as its meaning. Vector alone favours paraphrase; keyword alone favours exact wording.";

const DEPTH_HELP =
  "How much of the contract is put in front of the model as evidence. Deep reaches clauses buried in exhibits but costs more and can pull in tangential text. Shallow is faster and tighter.";

const SUGGESTIONS = [
  "Does the contract require MFA for privileged access?",
  "How often must the asset inventory be reconciled?",
  "Is background screening required for vendor staff?",
];

export function ChatView() {
  const { id } = useParams();
  const documentId = Number(id);
  const health = useHealth();
  const document = useDocument(Number.isInteger(documentId) ? documentId : null);
  const { turns, ask } = useChat();
  const field = useRef<HTMLInputElement>(null);

  // Session-scoped, not per document, and per *question* rather than per
  // conversation: they apply to the next question and do not re-run answers
  // already on screen.
  const models = health.data?.chat_models ?? [];
  const [model, setModel] = useState<string | null>(null);
  const [retrieval, setRetrieval] = useState<RetrievalMode | null>(null);
  const [depth, setDepth] = useState<Depth>("medium");

  const chosenModel = model ?? health.data?.answer_model ?? models[0] ?? "";
  const chosenRetrieval = retrieval ?? ((health.data?.retrieval_mode as RetrievalMode) ?? "hybrid");
  const configuredTopK = health.data?.retrieval_top_k ?? 6;

  const keyless = health.data?.key_present === false;
  const name = document.data?.filename ?? "this contract";

  const busy = useMemo(() => turns.some((turn) => turn.streaming || turn.working), [turns]);

  function send(question: string) {
    const text = question.trim();
    if (!text || keyless) return;
    void ask(text, {
      documentId,
      model: chosenModel,
      retrievalMode: chosenRetrieval,
      topK: topKFor(depth, configuredTopK),
    });
  }

  if (document.error) return <ErrorSurface error={document.error} />;

  return (
    <>
      <PageHead
        title="Ask this contract"
        subtitle="Every answer carries the clause and page it came from, checked against the source passage"
      />

      <NoKeyBanner />
      {Number.isInteger(documentId) ? <DocumentTabs documentId={documentId} /> : null}

      <div className={styles.page}>
        <div className={styles.settings}>
          {/* Rendered from /health's `chat_models`, which is the allowlist the
              API actually enforces -- so every option offered is an option
              that will be honoured. */}
          <Select
            label="Model"
            value={chosenModel}
            options={models.length > 0 ? models : [chosenModel]}
            onChange={setModel}
            width="232px"
          />
          <Select
            label="Retrieval"
            value={chosenRetrieval}
            options={RETRIEVAL_MODES}
            onChange={setRetrieval}
            help={RETRIEVAL_HELP}
            width="166px"
          />
          <Select
            label="Depth"
            value={depth}
            options={DEPTHS}
            onChange={setDepth}
            help={DEPTH_HELP}
            width="158px"
          />
          <div className={styles.spacer} />
          <p className={styles.note}>
            Applies to the next question · {chosenRetrieval} retrieval at {depth} depth over {name}
          </p>
        </div>

        {turns.length > 0 ? (
          <div className={styles.transcript}>
            {turns.map((turn) => (
              <TurnPair key={turn.id} turn={turn} name={name} onRetry={() => send(turn.question)} />
            ))}
          </div>
        ) : null}

        {/* A first-run affordance that stays visible: cheap, and they teach a
            reader what this is for. */}
        <div className={styles.chips}>
          {SUGGESTIONS.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              className={styles.chip}
              disabled={busy || keyless}
              onClick={() => send(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>

        <div className={styles.input}>
          <input
            ref={field}
            className={styles.field}
            placeholder="Ask anything about this contract"
            aria-label="Ask anything about this contract"
            disabled={keyless}
            // Uncontrolled: no re-render per keystroke, and the field keeps its
            // text when a question fails.
            onKeyDown={(event) => {
              if (event.key !== "Enter") return;
              event.preventDefault();
              const value = field.current?.value ?? "";
              if (!value.trim()) return;
              send(value);
              if (field.current) field.current.value = "";
            }}
          />
          <button
            type="button"
            className={styles.send}
            aria-label="Send"
            disabled={keyless}
            title={keyless ? NO_KEY_REASON : undefined}
            onClick={() => {
              const value = field.current?.value ?? "";
              if (!value.trim()) return;
              send(value);
              if (field.current) field.current.value = "";
            }}
          >
            <Icon name="send" size={17} weight={1.9} />
          </button>
        </div>

        <p className={styles.closing}>
          Answers are drawn only from {name}. Nothing outside the active document is retrieved.
        </p>
      </div>
    </>
  );
}

function TurnPair({ turn, name, onRetry }: { turn: Turn; name: string; onRetry: () => void }) {
  return (
    <>
      <article className={styles.turn}>
        <div className={`${styles.avatar} ${styles.avatarUser}`}>
          <Icon name="person" size={15} />
        </div>
        <div className={styles.block}>
          <span className={styles.who}>You</span>
          <p className={styles.text}>{turn.question}</p>
        </div>
      </article>

      <article className={styles.turn}>
        <div className={`${styles.avatar} ${styles.avatarAssistant}`}>
          <Icon name="document-check" size={15} />
        </div>
        <div className={styles.block}>
          <span className={styles.who}>Contract Analyzer</span>

          {turn.answer || turn.streaming ? (
            <AnswerText text={turn.answer} caret={turn.streaming} />
          ) : null}

          {/* The retrieval trail. Replaced by the answer, not appended to. */}
          {turn.working ? (
            <div className={styles.trail} aria-live="polite">
              {turn.tools.length === 0 ? (
                <span className={styles.trailLine}>searching {name}</span>
              ) : (
                turn.tools.map((call, index) => (
                  <span key={index} className={styles.trailLine}>
                    {describe(call, name)}
                  </span>
                ))
              )}
              <ProgressBar thin label="Retrieving" />
            </div>
          ) : null}

          {turn.citations.length > 0 ? (
            <div className={styles.cites}>
              {turn.citations.map((citation) => (
                <QuoteCard key={citation.evidence_id} quote={citation} tone="chat" />
              ))}
            </div>
          ) : null}

          {turn.done ? (
            <div className={styles.usage}>
              <span>
                {turn.elapsedS != null ? `${turn.elapsedS.toFixed(1)} s · ` : ""}$
                {turn.done.cost_usd.toFixed(3)} · {turn.done.model}
              </span>
              <span>
                {turn.done.tool_calls} tool call{turn.done.tool_calls === 1 ? "" : "s"}
              </span>
              <span>every quote checked against the source passage</span>
            </div>
          ) : null}

          {/* Neither of these is a spinner. An interrupted turn is a state you
              can see, with the way out attached. */}
          {turn.incomplete ? (
            <ErrorSurface
              error={incompleteError()}
              onRetry={onRetry}
            />
          ) : null}
          {turn.error ? <ErrorSurface error={turn.error} onRetry={onRetry} /> : null}
        </div>
      </article>
    </>
  );
}

/** Constructed rather than thrown by the client: the stream did not *fail*, it
 *  stopped. The distinction is the difference between "something is broken"
 *  and "ask again for the rest", and the reader deserves the right one. */
function incompleteError() {
  return new ApiError(
    0,
    "stream_incomplete",
    "The answer stopped before it finished.",
    "Whatever arrived is above. Ask again to get the rest.",
  );
}

/** The tool trail, in the user's words. `search` is the only tool chat has, so
 *  this is a small table rather than a general renderer. */
function describe(call: { name: string; args?: Record<string, unknown> | null; returned?: number | null }, name: string): string {
  if (call.name === "search") {
    const mode = String(call.args?.mode ?? "hybrid");
    const k = call.returned ?? call.args?.top_k;
    return `searching ${name} — ${mode} retrieval${k ? `, ${k} passages` : ""}`;
  }
  if (call.name === "read_section") {
    return `reading § ${String(call.args?.section ?? call.args?.title ?? "")}`.trim();
  }
  return `${call.name}`;
}
