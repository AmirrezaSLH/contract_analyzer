# Compliance

What `src/contract_analyzer/compliance/` holds: the five criteria, the
result schema, and the structural validator between the model's draft and
the evidence it retrieved.

## The criteria (`criteria.json`, `criteria.py`)

The assignment's five requirements, verbatim, with two additions per entry:
an `id` (`password_management`, `it_asset_management`,
`training_background_checks`, `data_in_transit`, `network_auth`) and the
`sub_requirements` its prose enumerates, each with a stable id:

```json
{"id": "mfa", "requirement": "MFA required for privileged/production access"}
```

Splitting the prose is what makes the state *derived* rather than asserted:
the model judges each sub-requirement, and the overall state must follow.
`get_criteria()` loads the file once and rejects duplicate ids;
`Criterion.question` is the assignment's description, which the result must
echo verbatim.

## The schema (`schemas.py`)

`ComplianceDraft` is what the model fills in through `output_config.format`:

| Field | |
|---|---|
| `compliance_question` | the criterion's question, copied exactly |
| `compliance_state` | `Fully Compliant` \| `Partially Compliant` \| `Non-Compliant` |
| `sub_requirements[]` | `id`, `requirement`, `status: met \| partial \| missing \| not_determined`, `quote_indexes[]` |
| `relevant_quotes[]` | `text` (verbatim, ≤ 300 chars), `evidence_id` (`E3`) |
| `rationale` | free text |
| `raw_confidence` | the model's own 0–1 estimate; one input to the number below |

The schema is a closed object -- `additionalProperties: false`, every field
required -- and carries **no** numeric or length constraints: structured
outputs do not enforce them, and a rule that is not enforced belongs in the
validator, where it can name what it found.

`ComplianceResult` is the draft after validation: each quote resolved to
`section_ref`, `page_display`, `chunk_id` and `verified`; `confidence` and
its `confidence_components`; `needs_review`, `unresolved_errors`,
`structure_rounds`, `ended_by`, `tool_calls`, `usage`, `cost_usd`, `model`.
`confidence_bucket` is High ≥ 0.75, Medium ≥ 0.5, Low.

## The validator (`validate.py`)

The API's constrained decoding guarantees the draft *parses*. The validator
exists for what a schema cannot say:

| Check | Why a schema cannot express it | Code |
|---|---|---|
| every `quote.evidence_id` is an `E` id in the ledger | runtime cross-reference | `unknown_evidence` |
| every quote is verbatim in that chunk | needs the chunk text | `not_verbatim` |
| `Fully` ⇔ all `met`; `Non-Compliant` ⇔ none `met`/`partial`; else `Partially` | cross-field rule | `inconsistent` |
| `met`/`partial` has ≥ 1 quote index; `missing`/`not_determined` has none | cross-field rule | `needs_quote`, `unexpected_quote` |
| quote indexes point into `relevant_quotes` | runtime range | `out_of_range` |
| sub-requirement ids are exactly the criterion's, once each | equality to an input | `ids` |
| `compliance_question` equals the criterion text | equality to an input | `not_verbatim` |
| no duplicate quotes; ≤ 300 chars; non-empty; rationale non-empty; `raw_confidence` in [0, 1] | sanity | `duplicate`, `too_long`, `empty`, `range` |

**Verbatim** is forgiving of what a PDF does to text, and of what the
chunker does to a table, and nothing else: NFKC, curly quotes and dashes
folded to ASCII, markdown `|` folded to a space, whitespace collapsed, case
folded, then a substring test against the chunk's `text_for_model()`.
"sixty (60)" for "ninety (90)" fails; `“default”` for `"default"` passes;
`GOV-01 Security governance program Annually` matches the grid row
`| GOV-01 | Security governance program | Annually |` -- a table chunk's
text *is* its grid, and exhibits are where the evidence lives.

Each failure is a `StructuralError(path, code, message)` whose message is the
feedback the model gets: what is malformed and where, **never what the answer
should be**. No model judges the structure. A model judges only content, and
that is Phase B's evaluator, a separate concern.

How the analysis uses it -- rounds, dropping, `needs_review`, the confidence
formula -- is in [generation.md](generation.md#surface-1-analysis-analysispy).

## The document runner (`report.py`, at the package root)

`route_criterion` answers one question -- Analyzer, Evaluator and the Router
that runs them, [agents/](agents/README.md). `analyze_document` is the layer
that knows a *contract-level* analysis exists. It is the **harness**, not a
fourth agent: threads, a connection per criterion, event serialisation and the
analyses row.

It lives at the top of the package, not in `compliance/`, because that is the
layer it belongs to: it uses `compliance` for the criteria and the result schema
and `generation` for the agent, and those two already refer to each other.
Putting the runner in either closes the loop -- importing `compliance` would
import the runner, which imports `generation.analysis`, which imports
`compliance.criteria`, which is still being imported. The import cycle was not a
quirk to route around; it was the module saying where it goes. `documents.py`
sits beside it for the same reason. It is the function the CLI calls and
the function the API's job worker calls -- the same arguments in both cases,
which is what makes "the API contains no logic the CLI does not have" a fact
rather than an intention.

```python
report = analyze_document(document_id, conn, embedder, settings, client,
                          criteria=None, on_event=None, cancelled=None,
                          workers=None)
```

It contains no prompting and no model logic. What it does contain is the three
consequences of running five agents at once:

**A connection per criterion, and the caller's is never touched.** The database
*path* is read once on the calling thread; each criterion opens its own
connection to it and closes it. `db.py` is explicit that concurrent use of one
connection from two threads is a bug and that `check_same_thread=False` only
stops sqlite3 from catching it -- and sharing would buy nothing, since SQLite
gives concurrent readers no parallelism on one connection. An in-memory
database has no path to reopen, so those runs are serial on the calling thread:
one connection, no pool, and the honest amount of parallelism available.

**The trace id carried across the pool.** `trace_id` lives in a `ContextVar`,
and `ThreadPoolExecutor.submit` does not copy the calling context. Every
submission goes through `contextvars.copy_context().run`. Without it, every
line the five agents emit -- `analysis.criterion`, `agent.call`, `agent.tool`,
every transport retry -- carries a null trace and the log stops reconstructing
the run. `tests/test_report.py` asserts the whole JSON log is free of null
trace ids, and fails when the `copy_context()` is removed.

**Events tagged, and delivered one at a time.** The agent loop emits
`tool_call` with no criterion on it, because at that level there is only one;
five interleaved runs would be unattributable. The runner stamps `criterion` on
every event, and holds a lock while calling `on_event` -- so a caller's
callback is **never invoked concurrently and never needs a lock of its own**.
The CLI prints, the API fans out to its SSE subscribers, neither has to think
about it.

Cancellation is honest rather than aspirational. `cancelled()` is polled before
each criterion starts, so it skips whatever has not begun and the report lists
those ids in `skipped` with `status="cancelled"`. At `workers >= len(criteria)`
everything starts at once and there is nothing left to skip: cancel then only
stops a job still waiting for a free worker. Stopping a *running* criterion
would mean threading the flag into the agent loop between tool calls, which is
a change to `generation/`, not to this file.

### The report

`AnalysisReport` is a pydantic model, so **the report on disk is the report
over the wire** -- no second schema between `scripts/analyze.py --out` and the
API's `GET /analyses/{id}`. `results` are in criteria order rather than
finishing order, so two runs of the same contract diff line by line.
`AnalysisTotals` sums the run: job duration, cost, tokens, tool calls, how many
results need review, how many were stopped by a counter, the mean confidence,
and how the Router closed each criterion out (`accepted`, `revised`,
`fallback`, `unevaluated`, `evaluator_cost_usd`) -- the KPI page's row for this
analysis. `cross_criterion_notes` is filled by the Router's fan-in pass
([agents/router.md](agents/router.md)); the field was present and empty long
before that pass existed, so its arrival changed no wire format.

### The CLI

```
make analyze F="data/samples/Sample Contract.pdf"
python scripts/analyze.py --document-id 3 --criteria password_management
```

A path is ingested first (unchanged files cost nothing -- `ingest_file` skips
on the content hash); `--document-id` analyses what is already stored. Progress
prints as it happens, one line per tool call with its arguments and one per
verdict, each tagged with the criterion it belongs to. The report is written as
JSON to `.run/analysis-<id>.json` unless `--out` says otherwise. Exit code 2
means the run was cancelled or incomplete, 1 that it could not start.
