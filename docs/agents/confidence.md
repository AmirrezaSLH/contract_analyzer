# Confidence

**The number `ComplianceResult.confidence` carries is a heuristic score. It is
not calibrated, and nothing in the UI or the docs should imply that it is.**

That sentence is the whole point of this page. Three different ideas hide
behind the word "confidence", and conflating them is the easiest way for a
system like this to mislead the person reading it.

## Three ideas, untangled

1. **A score** -- one number in [0, 1] attached to one verdict: "how likely is
   this compliance state to be right?" It is a *claim*. A score can exist
   without any statistics behind it, and this one does.
2. **A calibrated score** -- the claim, checked: among all results scored ~0.8,
   about 80% were actually right. Calibration is a property **measured over
   many labelled results**, never visible in a single one. Until labels exist,
   no score is calibrated, whatever the formula looks like.
3. **An interval** -- a range with a coverage guarantee ("the accept rate is
   0.72, 95% CI [0.61, 0.81]"). Intervals belong naturally to the KPI
   dashboard's aggregate rates, and, in a more advanced form, to a single
   verdict as a *prediction set*. Not the same thing as the per-result score.

What ships today is (1), honestly labelled. (2) and (3) are staged in
`plan_implement_docs/AGENT_PLAN_01/05_confidence_plan.md`.

## What the formula is

Composed by the Router at `finalize`, from `analysis.compute_confidence`:

```
confidence = min(raw_confidence, critic_confidence)   two independent estimates,
                                                      take the pessimist
           × quote_term                                did the evidence carry the claim
           × (1 − not_determined / total)              how much of the criterion settled
           × (1.0 if the critic agrees on the state else 0.6)

capped at 0.5 when the result needs review or a counter ended the run
clamped to [0.05, 0.95] because nothing here is certain either way
```

Each term is a sentence, and each is stored separately in
`confidence_components`. **That storability is the design**: a later phase can
fit the *combination* against real labels without changing the schema or
re-running anything, and every demo run made before calibration exists still
contributes its data afterwards.

### `min(analyst, critic)`

Two independent estimates of the same event. Taking the pessimist is the
cheap, conservative fusion. Whether it is *too* conservative is exactly the
kind of question a reliability curve answers and an argument does not.

### `quote_term`

The critic's **support ratio** when there is a critic, and the verbatim ratio
otherwise. Verbatim-ness was always a proxy for support -- it was what could be
checked without a reader. Now that a reader judges support directly, the proxy
steps aside. It remains a hard gate upstream in `validate.py`, where a
non-verbatim quote is dropped before it can be judged at all.

### The agreement factor

0.6, not 0. Two careful readers disagreeing means the answer is uncertain, not
that the analyst's is wrong. The Router never overrules the state; it lowers
the confidence and says so.

### The components dict

The `critic` and `agreement` keys are present **only when a critic actually
spoke**. A run that ended `unevaluated` has neither, so its components can
never be misread as "the critic agreed".

## What changed when the Evaluator landed

Scores generally drop, especially where the critic disagrees. That is the
point. But it means historical KPI comparisons cross a regime boundary: the
metrics keep flowing, and the dashboard should annotate the change rather than
pretend continuity. `verdict` and the components make the boundary
identifiable per result rather than only by date.

## What is *not* here yet, and what it would take

| Stage | What | Gate |
|---|---|---|
| Wilson intervals on every KPI proportion | ~10 lines and a test; every rate carries its n and its interval | nothing -- purely additive |
| Reviewer overrides | a `reviews` table, one POST endpoint, two buttons; a confirm is a positive label, a change is a negative one | nothing |
| Gold set | `gold.json` per fixture contract, `make calibrate`; doubles as the prompt-change regression suite | nothing |
| Reliability diagram, ECE, Brier | measured calibration, per bucket, with intervals so an n=4 bucket cannot masquerade as evidence | needs labels |
| Isotonic or Platt mapping applied at `finalize` | the score becomes calibrated, versioned, and segmentable by `calibration_version` | ~100–150 labels (≥15 per bucket) |
| Conformal prediction sets | "{Partially, Non-Compliant} @ 90%", distribution-free coverage | the same label bar |

Volume expectation, stated so nobody over-promises: five labels per reviewed
contract. Until the bar is cleared the KPI page shows the counts and says
**"insufficient labels to calibrate"** -- implying otherwise would be the bug.

## The buckets

`confidence_bucket` (High ≥ 0.75, Medium ≥ 0.5, Low) are **provisional
labels, not probabilities**. When a mapping is eventually fitted, the
thresholds get re-read off the fitted curve at the same moment.

## Questions this page exists to answer

* *"Is the confidence calibrated?"* -- No. It is an honest heuristic with its
  components stored. Calibration is a measured property, so the system ships
  the measurement machinery and applies a fitted mapping once n clears the
  bar. Until then the UI says uncalibrated.
* *"Why `min` and not an average?"* -- Two independent estimates of one event;
  the pessimist is the conservative fusion, and the reliability curve will say
  whether it is too conservative.
* *"Where are the confidence intervals?"* -- On aggregate rates, where a
  proportion over n runs actually has one. Per-verdict, the principled version
  is a conformal prediction set with guaranteed coverage: designed, and gated
  on labels.
