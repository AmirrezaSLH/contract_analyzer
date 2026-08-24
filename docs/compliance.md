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
