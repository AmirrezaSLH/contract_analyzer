# Evaluator

`src/contract_analyzer/generation/evaluator.py`. The critic. It is handed the
quotes and the claims, and nothing else.

```python
from contract_analyzer.generation import evaluate

evaluation = evaluate(request, criterion, settings=settings, client=client)
evaluation.findings.state_agreement      # agree | disagree
evaluation.findings.support_ratio        # how much of what was claimed was carried
evaluation.findings.missing_searches     # ids called missing without a search
evaluation.cost_usd
```

## Why it sees so little

The one failure the Analyzer structurally cannot catch is a **verbatim quote
that does not support the claim**. "Supplier *may* rotate passwords" is copied
exactly from the contract, passes every rule in `validate.py`, and supports
nothing.

A critic shown the Analyzer's conversation inherits the reasoning that made
the mistake. A critic shown only the quote, the passage it came from and the
claim has to re-derive the support link -- which is exactly the check that was
missing. The Router enforces that blindness when it builds the request, and
`EvaluationRequest` has no field for a conversation, so it cannot be violated
silently.

## E1 -- deterministic pre-checks (`precheck`)

Python checks what Python can check exactly. The results are attached to the
request as facts for the critic, and to the findings as flags.

**Search coverage.** For each sub-requirement marked `missing`: did any tool
call in the run actually go looking for it? A fact about the log, not a
judgement -- so it reaches the Router whether or not the critic call ever
succeeds, and the critic may *add* to those facts but never remove one.

Plain token overlap does not work here, and finding that out is instructive:
every sub-requirement of a password criterion contains the word "password", so
any query at all would "cover" all five and the check could never fire. So the
comparison is against **what separates a sub-requirement from its siblings** --
the criterion's own requirement text and the other sub-requirements are
subtracted, and what remains ("vaulting", "privileged") is what a search would
have had to go after to have looked for this one specifically. A four-rule
stemmer makes "rotated", "rotation" and "rotate" the same word, because
calling that unsearched would be a false accusation dressed as a fact.

**Hedge lexicon.** Quotes carrying "commercially reasonable", "where
feasible", "best efforts", "may", "industry standard" under a `met` or
`partial` status are flagged **for the critic to examine**. A flag, not an
error: the lexicon cannot tell a carve-out from a subordinate clause, and does
not try. Quotes under a `missing` status are not flagged -- hedging only means
something where an obligation was claimed.

## E2 -- the critic call

One structured call, same client, same transport, `output_config.format` on
the findings schema, **no tools**. The request goes into the prompt as JSON --
`_render` dumps the model -- so what the critic reads is byte-for-byte what an
out-of-process critic would be POSTed.

The rubric asks it to read like a lawyer and separate three things:

* **obliges** -- "shall", "must", "is required to";
* **permits** -- "may", "at its discretion";
* **describes** -- "currently uses", which binds nothing going forward.

`met` needs an obligation with no material carve-out; hedged, scoped or
best-efforts language is at most `partial`; `irrelevant` is for a quote about
something else however exactly it was copied; `contradicts` for one that says
the opposite. It has no retrieval and is told not to import outside knowledge
of what contracts usually say -- "most vendors would also cover this" is not
evidence about *this* contract.

## What comes back, and what is checked

```python
class EvaluatorFindings(_Strict):
    quote_support: list[QuoteSupport]        # supports | partial | irrelevant | contradicts
    status_agreement: list[StatusAgreement]  # agree | too_strong | too_weak
    state_agreement: Literal["agree", "disagree"]
    missing_searches: list[str]
    critic_confidence: float
    notes: str
```

Constrained decoding guarantees the **shape** -- the keys, the types, the
enums -- and nothing about whether the values refer to anything. So
`validate_findings` checks in Python: quote indexes in range, sub-requirement
ids known, no sub-requirement judged twice, confidence in [0, 1]. Findings
that point at quote 7 of a three-quote draft are not findings, and count as a
failed attempt.

One judgement per **(quote, sub-requirement) pair**: a quote cited for two
sub-requirements is judged twice, because the unit being scored is a claim,
not a string. `support_ratio` scores `partial` at a half -- a hedged quote is
evidence, just not whole evidence, and scoring it zero would punish an analyst
who correctly answered `partial` with correctly partial language.

## Failure, and the backoff ladder

Transport failures -- connection, timeout, 429, 5xx -- are already retried
with full-jitter exponential backoff by `http_client.RetryingTransport`, the
process's single retry loop. **This module deliberately does not run a second
policy against the same server**; one-transport is the rule, and a test pins
that four transport attempts do not become twelve.

What it does retry is *semantic* failure: truncation, refusal, JSON that does
not parse, findings that fail the deterministic checks. Two more attempts on
the same curve (`backoff_delay`, imported rather than reimplemented, base
0.5 s so the worst case is ~1.5 s), because the load-shaped failures clear
when the load does. A structural violation will not fix itself in the wait,
and that is a second and a half well spent against the alternative of shipping
a criterion nobody criticised.

When the ladder is exhausted, `EvaluationFailed`. The Router degrades to
`verdict="unevaluated"`: the analysis ships, flagged, with the analyst's own
numbers and no fabricated agreement. **The Evaluator may only ever lower what
ships. It must never be the reason nothing ships.**

## What it does *not* decide

Nothing in `EvaluatorFindings` says what happens next. Whether a disputed
quote is worth another round, and which kind, is the Router's decision policy.
The findings are evidence; the authority is elsewhere. They land on the final
result whatever the Router decided -- including on `accept` -- so the UI can
show why the confidence is what it is.

## Spans

`evaluator.precheck` (hedged quotes, unsearched ids) and `evaluator.critic`
(model, tokens, cost, disputed counts, support ratio, state agreement,
`critic_confidence`, attempts). Both carry the trace id, so the demo can show
the critic disagreeing in `.run/app.jsonl` right next to the analysis turn it
disagreed with.

## Tested by

`tests/test_evaluator.py` -- the coverage check firing and not firing, the
stemmer, the hedge flags reaching the JSON the critic saw, the isolation
asserted on the wire, every deterministic check, the retry ladder and its
growing waits, and the one-transport assertion.
