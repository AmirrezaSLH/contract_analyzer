"""The agent loop: `while stop_reason == "tool_use"`, with three counters.

This is the whole of the "Router Agent". The model is given the system
prompt, the task's messages and the two tool definitions; while it asks for
tools, each call is executed and its result appended; when it stops asking,
the surface's `finisher` produces the final turn from the conversation and
the evidence ledger. No framework: a LangGraph would hide exactly the part
that has to be walked through in the log during the demo, and would bring a
second retry layer against the project's one-transport rule.

**Not getting stuck is enforced by counters, not prompts.**

* `max_tool_calls` -- executions per run (analysis 8 per criterion, chat 4);
* `max_evidence_tokens` -- the ledger's total; once reached, tool results say
  so (`tools.py`) and the loop stops offering tools;
* dedupe -- an identical call is answered from the ledger at zero retrieval
  cost (`tools.py`), so a model that repeats itself runs out of calls without
  burning the index.

On any cap the finisher is invoked with what exists and the run is marked
`ended_by="cap"` rather than `"model"`; the confidence and the KPI page both
read that field.

**Every request is a span.** `agent.call` carries surface, turn, model,
effort, tokens and cost; `agent.tool` (in `tools.py`) carries name, mode,
`top_k`, returned and new. Reconstructing a run from `.run/app.jsonl` is the
demo's live-log walkthrough.

**No request sends `thinking`.** On this model thinking is on by default,
`budget_tokens` is a 400, and disabling it has documented failure modes.
`output_config.effort` is the one lever, and it is a per-surface setting.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

from ..config import Settings, get_settings
from ..logger import get_logger, span
from .client import Usage, api_errors, get_client
from .tools import ContractTools, Evidence, ToolCall

log = get_logger(__name__)

T = TypeVar("T")
Event = dict[str, Any]
OnEvent = Callable[[Event], None]


@dataclass
class AgentTask:
    """What one run is asked to do, and how hard it may try."""

    surface: str
    system: str
    messages: list[dict[str, Any]]
    effort: str
    max_tool_calls: int


@dataclass
class AgentRun:
    """Everything a run produced, for the finisher, the result and the log."""

    surface: str
    model: str
    effort: str
    messages: list[dict[str, Any]]
    evidence: Evidence
    tool_calls: list[ToolCall]
    usage: Usage = field(default_factory=Usage)
    #: `model` when it stopped asking for tools; `cap` when a counter stopped
    #: it; otherwise the stop reason (`max_tokens`, `refusal`).
    ended_by: str = "model"
    turns: int = 0
    #: What the finisher returned.
    result: Any = None

    @property
    def cost_usd(self) -> float:
        return self.usage.cost(self.model)


def content_params(message: Any) -> list[dict[str, Any]]:
    """A response's content as request params, thinking blocks included.

    Thinking blocks go back unchanged -- their signature is what lets the
    model continue its own reasoning on the next turn.
    """
    return [block.model_dump(mode="json", exclude_none=True) for block in message.content]


def text_of(message: Any) -> str:
    return "".join(block.text for block in message.content if block.type == "text")


def call_model(
    client: Any,
    *,
    model: str,
    max_tokens: int,
    system: str,
    messages: Sequence[dict[str, Any]],
    effort: str,
    surface: str,
    turn: int,
    tools: Sequence[dict[str, Any]] | None = None,
    tool_choice: dict[str, Any] | None = None,
    format: dict[str, Any] | None = None,
    on_text: Callable[[str], None] | None = None,
) -> Any:
    """One request, streamed, logged as `agent.call`. Returns the final message.

    Streamed even when nobody watches the deltas: a long turn then keeps
    bytes moving under the transport's read timeout instead of racing it.

    A finisher that wants no more tool calls keeps the definitions and sends
    `tool_choice={"type": "none"}` rather than dropping them: the request then
    shares the loop's exact `tools -> system` prefix, which is what prompt
    caching keys on, and a conversation carrying tool blocks stays valid
    whatever the API's rule on undefined tools is that week (a live probe on
    2026-08-24 accepted both shapes).
    """
    output_config: dict[str, Any] = {"effort": effort}
    if format is not None:
        output_config["format"] = format
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": system,
        "messages": list(messages),
        "output_config": output_config,
    }
    if tools:
        kwargs["tools"] = list(tools)
    if tool_choice is not None:
        kwargs["tool_choice"] = tool_choice
    with (
        span("agent.call", log, surface=surface, turn=turn, model=model, effort=effort) as bag,
        api_errors(),
        client.messages.stream(**kwargs) as stream,
    ):
        if on_text is not None:
            for text in stream.text_stream:
                on_text(text)
        message = stream.get_final_message()
        usage = Usage.from_message(message)
        bag.update(
            stop_reason=message.stop_reason,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=usage.cost(model),
            structured=format is not None,
        )
    return message


def run_agent(
    task: AgentTask,
    *,
    tools: ContractTools,
    finisher: Callable[[AgentRun], T] | None = None,
    settings: Settings | None = None,
    client: Any = None,
    model: str | None = None,
    on_event: OnEvent | None = None,
) -> AgentRun:
    """Drive the loop, then hand the run to `finisher`. Raises before any request
    if there is no key (`AnswerUnavailable`).

    `model` overrides `settings.answer_model` for this run and nothing else --
    the caller that passes it is answering one question, not reconfiguring the
    process. It is recorded on the run, so `AgentRun.model` is always the model
    that actually answered rather than the one that was configured.
    """
    settings = settings or get_settings()
    client = client or get_client(settings)
    emit = on_event or (lambda event: None)
    run = AgentRun(
        surface=task.surface,
        model=model or settings.answer_model,
        effort=task.effort,
        messages=list(task.messages),
        evidence=tools.evidence,
        tool_calls=tools.calls,
    )
    definitions = tools.definitions()

    with span("agent.run", log, surface=task.surface, max_tool_calls=task.max_tool_calls) as bag:
        while True:
            run.turns += 1
            message = call_model(
                client,
                model=run.model,
                max_tokens=settings.answer_max_tokens,
                system=task.system,
                messages=run.messages,
                effort=task.effort,
                surface=task.surface,
                turn=run.turns,
                tools=definitions,
            )
            run.usage += Usage.from_message(message)
            run.messages.append({"role": "assistant", "content": content_params(message)})
            preamble = text_of(message)
            if preamble:
                emit({"type": "text", "surface": task.surface, "text": preamble})

            if message.stop_reason != "tool_use":
                run.ended_by = "model" if message.stop_reason == "end_turn" else str(
                    message.stop_reason
                )
                break

            results = []
            for block in message.content:
                if block.type != "tool_use":
                    continue
                outcome = tools.execute(block.name, dict(block.input or {}))
                call = outcome.call
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": outcome.text,
                        "is_error": call.error is not None,
                    }
                )
                emit(
                    {
                        "type": "tool_call",
                        "surface": task.surface,
                        "name": call.name,
                        "args": call.args,
                        "returned": call.returned,
                        "new": call.new,
                        "ids": call.ids,
                        "error": call.error,
                    }
                )
            # All results in one user turn: splitting them teaches the model
            # to stop making parallel calls.
            run.messages.append({"role": "user", "content": results})

            if len(tools.calls) >= task.max_tool_calls or tools.budget_reached:
                run.ended_by = "cap"
                break

        bag.update(
            turns=run.turns,
            tool_calls=len(tools.calls),
            evidence=len(tools.evidence),
            evidence_tokens=tools.evidence.tokens,
            ended_by=run.ended_by,
            input_tokens=run.usage.input_tokens,
            output_tokens=run.usage.output_tokens,
        )
        if finisher is not None:
            run.result = finisher(run)
        bag["cost_usd"] = run.cost_usd
    return run


__all__ = ["AgentRun", "AgentTask", "call_model", "content_params", "run_agent", "text_of"]
