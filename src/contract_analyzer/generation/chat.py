"""Surface 2: the cited chat. The same loop, a streamed finisher with citations.

The model searches through the tools at `chat_effort`; when it stops, the
**chat finisher** sends one request with a document block per ledger entry,
citations on, no tools, no `format` -- `format` and citations cannot share a
request -- and streams the answer. The quotes come from the API, extracted
from the passages we sent, so they are verbatim by construction.

If the ledger is empty after the loop's searches, the answer is
`chat.no_context` and the finisher makes no call: the model has already
looked and found nothing, and a second request over nothing would only
invite it to answer from general knowledge.

History is replayed as plain text only. Previous turns' passages and tool
traffic are not re-sent; the current turn re-retrieves through the tools,
so a follow-up stays grounded in what it actually found. Last 8 messages.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..config import Settings, get_settings
from ..embeddings.base import Embedder
from ..logger import get_logger, span
from .agent import AgentRun, AgentTask, OnEvent, call_model, run_agent
from .blocks import Citation, answer_text, document_blocks, resolve_citations
from .client import Usage, get_client
from .prompts import get_prompts
from .tools import ContractTools, Evidence, ToolCall

log = get_logger(__name__)

#: Messages of history replayed. Enough for a follow-up chain; small enough
#: that a long session does not drag every earlier answer into every call.
HISTORY_LIMIT = 8

Message = dict[str, str]


@dataclass
class AnswerResult:
    text: str
    citations: list[Citation]
    evidence: Evidence
    tool_calls: list[ToolCall]
    usage: Usage
    cost_usd: float
    model: str
    #: The finisher's stop reason; `no_context` when it never ran.
    stop_reason: str
    ended_by: str
    turns: int = 0
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def grounded(self) -> bool:
        return self.stop_reason != "no_context"


def chat(
    question: str,
    conn: sqlite3.Connection,
    embedder: Embedder | None = None,
    settings: Settings | None = None,
    *,
    document_id: int,
    history: Sequence[Message] = (),
    client: Any = None,
    on_text: Callable[[str], None] | None = None,
    on_event: OnEvent | None = None,
) -> AnswerResult:
    """Answer one question about one contract, cited. Raises `AnswerUnavailable`
    before any request if there is no key."""
    settings = settings or get_settings()
    client = client or get_client(settings)
    prompts = get_prompts(settings)
    tools = ContractTools(conn, document_id=document_id, embedder=embedder, settings=settings)
    system = prompts.get("agent.system") + "\n\n" + prompts.get("chat.system")
    replay = replay_history(history)
    task = AgentTask(
        surface="chat",
        system=system,
        messages=[*replay, {"role": "user", "content": question}],
        effort=settings.chat_effort,
        max_tool_calls=settings.chat_max_tool_calls,
    )

    def finisher(run: AgentRun) -> AnswerResult:
        if len(run.evidence) == 0:
            text = prompts.get("chat.no_context")
            log.info("chat.no_context", extra={"tool_calls": len(run.tool_calls)})
            if on_text is not None:
                on_text(text)
            return AnswerResult(
                text=text, citations=[], evidence=run.evidence, tool_calls=run.tool_calls,
                usage=run.usage, cost_usd=run.cost_usd, model=run.model,
                stop_reason="no_context", ended_by=run.ended_by,
            )
        run.turns += 1
        message = call_model(
            client,
            model=run.model,
            max_tokens=settings.answer_max_tokens,
            system=system,
            messages=[
                *replay,
                {
                    "role": "user",
                    "content": [
                        *document_blocks(run.evidence),
                        {"type": "text", "text": question},
                    ],
                },
            ],
            effort=settings.chat_effort,
            surface="chat",
            turn=run.turns,
            on_text=on_text,
        )
        run.usage += Usage.from_message(message)
        citations = resolve_citations(message, run.evidence)
        return AnswerResult(
            text=answer_text(message), citations=citations, evidence=run.evidence,
            tool_calls=run.tool_calls, usage=run.usage, cost_usd=run.cost_usd,
            model=run.model, stop_reason=str(message.stop_reason), ended_by=run.ended_by,
        )

    with span("chat", log, document_id=document_id, history=len(replay)) as bag:
        run = run_agent(
            task, tools=tools, finisher=finisher, settings=settings, client=client,
            on_event=on_event,
        )
        result: AnswerResult = run.result
        result.turns = run.turns
        bag.update(
            grounded=result.grounded,
            evidence=len(result.evidence),
            citations=len(result.citations),
            tool_calls=len(result.tool_calls),
            cost_usd=result.cost_usd,
        )
    return result


def replay_history(history: Sequence[Message]) -> list[Message]:
    """The last `HISTORY_LIMIT` turns as plain text, roles alternating as given."""
    kept = [
        {"role": m["role"], "content": str(m["content"])}
        for m in history
        if m.get("role") in ("user", "assistant") and str(m.get("content", "")).strip()
    ]
    return kept[-HISTORY_LIMIT:]


__all__ = ["HISTORY_LIMIT", "AnswerResult", "chat", "replay_history"]
