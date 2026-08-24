# Confidence — score, calibration, and intervals: a plan

**Status: Phase A implemented**; Phases B, C and D are not started. The formula, the stored components and the honest "uncalibrated" naming shipped with the three agents; `docs/agents/confidence.md` is the user-facing version and names the gate on each later phase.

**Why this doc exists.** "Confidence" hides three different ideas, and the
assignment (§3.2 "confidence calibration", §7 "a calibrated confidence
score") plus the KPI requirement touch all three. This doc separates them,
says which one each part of the system needs, and stages the work so every
stage ships something defensible on its own.

## 0. The three ideas, untangled

1. **A confidence *score*** — one number in [0, 1] attached to a single
   verdict: "how likely is this compliance state to be right?" This is
   what `ComplianceResult.confidence` is today. A score can exist without
   any statistics; it is a claim.
2. **A *calibrated* score** — the claim is checked: among all results the
   system scored ~0.8, about 80% were actually right. Calibration is a
   *property measured over many labelled results*, never visible in a
   single one. Until labels exist, no score is calibrated, whatever the
   formula looks like — and the KPI page must say so rather than imply it.
3. **A confidence *interval*** — a range with a coverage guarantee, e.g.
   "the accept rate is 0.72, 95% CI [0.61, 0.81]". Intervals apply
   naturally to the **KPI dashboard's aggregate rates** (proportions over
   n runs), and — in a more advanced form (conformal prediction) — to a
   single verdict as a *prediction set* ("{Partially, Non-Compliant} with
   90% coverage"). They are not the same thing as the per-result score.

The plan: keep the score honest (Phase A), get labels (Phase B), measure
and then fit calibration (Phase C), put real intervals where they belong —
the dashboard first, per-verdict prediction sets only if wanted (Phase D).

## Phase A — the honest heuristic score (ships with the three agents)

What exists after AGENT_PLAN_01 lands (master plan §5):

```
confidence = min(raw_confidence, critic_confidence)        # two independent estimates,
                                                           # take the pessimist
           × support_ratio                                 # evidence actually supports;
                                                           # supports=1, partial=0.5,
                                                           # irrelevant/contradicts=0,
                                                           # per (quote, sub-requirement)
           × (1 − not_determined / total)                  # coverage of sub-requirements
           × (1.0 if critic agrees on state else 0.6)
capped at 0.5 on fallback / unevaluated / ended_by == "cap"
clamped to [0.05, 0.95]                                    # nothing here is certain
```

Each term is a sentence, each is stored in `confidence_components`, and
that storability is the design: a later phase re-fits the *combination*
without changing the schema or re-running anything. What Phase A must
also do, and costs nothing:

* **Name it honestly.** UI copy and docs call it a *heuristic confidence
  score (uncalibrated)*. The buckets (`High ≥ 0.75`, `Medium ≥ 0.5`,
  `confidence_bucket` in `compliance/schemas.py`) are provisional labels,
  not probabilities.
* **Log every component** (already done via the result JSON in the
  analyses store) so Phase C has its training data retroactively — every
  demo run before calibration exists still contributes labels later.

## Phase B — labels, the ingredient everything downstream needs

Calibration and intervals are impossible without ground truth. Two
sources, cheapest first:

1. **Reviewer override in the UI** (planned since 04_02). On each result
   card: *Confirm* / *Change state* (to one of the other two). Stored per
   `(analysis_id, criterion_id)`: the shown state, the shown confidence,
   the reviewer's state, timestamp. One new table (`reviews`), one POST
   endpoint, two buttons — deliberately minimal. A confirm is a positive
   label; a change is a negative one for the shown state.
2. **A gold set.** The repo's fixture contracts (and any synthetic ones
   generated for testing) get a `gold.json`: criterion → expected state,
   authored once by reading the contract. `make calibrate` runs the
   pipeline over the gold set and scores it. This is also the regression
   suite for prompt changes — two birds.

Label schema (shared by both sources):

```json
{"analysis_id": "…", "criterion_id": "c3", "predicted": "Partially Compliant",
 "confidence": 0.71, "label": "Non-Compliant", "source": "reviewer|gold", "at": "…"}
```

Volume expectation, stated so nobody over-promises: 5 labels per reviewed
contract; meaningful calibration curves start around ~100–150 labels
(≥ ~15 per confidence bucket). Until then the KPI page shows the counts
and says "insufficient labels to calibrate".

## Phase C — measure calibration, then (and only then) fit it

### C1. Measure (no model changes; pure reporting)

On the KPI page, computed from the joined `results × labels`:

* **Reliability diagram** — bucket predictions by confidence
  (e.g. [0–0.2, …, 0.8–1.0]), plot *mean confidence* vs *observed
  accuracy* per bucket. The identity diagonal is perfect calibration;
  points below it mean over-confidence.
* **ECE (expected calibration error)** — the one-number summary:
  `ECE = Σ_b (n_b / N) · |accuracy_b − mean_confidence_b|`. Threshold to
  defend in the interview: ECE ≤ 0.10 acceptable, > 0.15 pages someone.
* **Brier score** — `mean((confidence − correct)²)` where `correct` is
  1/0 against the label; tracks both calibration *and* discrimination,
  and one number per week on the trend view shows drift.

Each bucket's observed accuracy gets a **Wilson interval** (see Phase D)
so a bucket with n=4 doesn't masquerade as evidence.

### C2. Fit (a mapping, not a retrain)

Once labels clear the volume bar, fit a **monotone mapping** from the
heuristic score to observed accuracy — **isotonic regression** (no shape
assumed, needs ~100+ points) or **Platt scaling** (logistic; works
smaller, assumes a sigmoid shape). Choose isotonic if volume allows, Platt
as the small-data fallback. Mechanics, kept boring:

* Fit offline (`scripts/fit_calibration.py`), output a JSON mapping
  (breakpoints or two Platt coefficients) checked into `data/` or the
  settings dir with a `calibration_version` and its fit date + n.
* At runtime, `finalize` applies the mapping *after* the heuristic:
  `confidence = calibrate(heuristic)`; both raw heuristic and calibrated
  value are stored (components again). `calibration_version` lands on the
  result, so the KPI page can segment by regime.
* Refit is manual and versioned — a cron-refit that silently moves the
  meaning of 0.8 is drift you inflicted on yourself. Bucket thresholds
  (0.75/0.5) are re-read from the fitted curve at the same moment.
* **Guard:** the mapping is only trusted inside the label distribution it
  was fitted on; per-criterion sample sizes shown next to the curve. Do
  not fit per-criterion mappings until each criterion has its own ~100
  labels — one global mapping first.

## Phase D — intervals, where they actually belong

### D1. The KPI dashboard's rates (do this; small and immediately honest)

Every proportion tile — accept rate, revise rate, needs-review share,
override rate, bucket accuracy — is currently a point estimate over
whatever n the window holds, and demo windows are small. Give each the
**Wilson score interval** (better than the normal approximation at small
n and near 0/1, which is exactly where these rates live):

```
center = (p̂ + z²/2n) / (1 + z²/n)
half   = z·√(p̂(1−p̂)/n + z²/4n²) / (1 + z²/n)      z = 1.96 for 95%
```

Pure function in `metrics` (≈10 lines + tests), rendered as a "0.72
[0.61–0.81], n=41" subtitle on tiles and as bands on the timeseries.
For **mean latency/cost** trend lines, a bootstrap percentile interval
(resample the window's runs 1000×, take the 2.5/97.5 percentiles) — the
distributions are skewed, so ±1.96·SE would lie.

This is the cheapest deliverable in the whole doc and instantly upgrades
the "defend every metric" conversation: every tile carries its own
uncertainty and its n.

### D2. Per-verdict prediction sets — conformal prediction (optional, only with labels)

If a per-result *interval-like* statement is wanted, the right tool for a
3-class verdict is **split conformal prediction**: on a held-out labelled
set, compute each example's nonconformity score (1 − confidence assigned
to the true state), take the (1−α) quantile q̂, and at inference return
*every state whose score ≤ q̂*. Guarantee: the set contains the true state
with probability ≥ 1−α, distribution-free, regardless of how bad the
underlying scores are. Output reads "{Partially, Non-Compliant} @ 90%".
Requires per-state scores (the analyst emits one `raw_confidence`; the
critic's `state_agreement` gives a second signal — a cheap per-state score
is the softmax-like spread derived from the components, or an ensemble
vote, next paragraph). Park it behind the same label bar as C2; it is a
strong interview answer even as a design ("here is how I would produce a
guaranteed-coverage set"), and a misleading feature if shipped unlabelled.

### D3. Self-consistency ensemble (optional, orthogonal)

Run the analysis finisher n=3 at higher temperature on a cheaper model;
the vote split (3–0, 2–1) is an empirical stability signal, and the
agreement fraction can feed the score or the conformal scores in D2.
Costed at ~2 extra cheap finisher calls per criterion. Only if the
reliability curve says the analyst+critic pair is miscalibrated in a way
the mapping cannot fix — an ensemble is a measurement, not a first resort.

## Order of work and effort

| Step | What | Est. | Ships value alone? |
|---|---|---|---|
| 1 | D1 Wilson + bootstrap intervals on KPI tiles/timeseries | 1 h | yes — immediately |
| 2 | Phase A naming/copy ("heuristic, uncalibrated") | 0.5 h | yes — honesty |
| 3 | B1 reviewer override: table, endpoint, two buttons | 2 h | yes — starts label flow |
| 4 | B2 gold set + `make calibrate` runner | 2 h | yes — regression suite too |
| 5 | C1 reliability diagram, ECE, Brier on KPI page | 2 h | yes — measures the gap |
| 6 | C2 isotonic/Platt fit + versioned mapping at `finalize` | 2 h | needs ~100 labels |
| 7 | D2 conformal sets | 3 h | design-only until labels |

Steps 1–5 are the demo story: *"the score is heuristic and labelled as
such; here is the machinery already collecting the evidence to calibrate
it, here is the curve with its intervals, and here is exactly what flips
on when n crosses the bar."* That is a stronger senior answer than a
fitted curve over 12 points would be.

## Where it goes

| File | What |
|---|---|
| `src/contract_analyzer/analyses.py` | `reviews` table + upsert/read |
| `api/routes/analyses.py` | `POST /analyses/{id}/criteria/{cid}/review` |
| `api/routes/metrics.py` | Wilson/bootstrap helpers; `/calibration` endpoint (buckets, ECE, Brier, n) |
| `generation/router.py` (`finalize`) | apply versioned mapping when present |
| `scripts/fit_calibration.py`, `data/calibration.json` | offline fit, versioned artifact |
| `data/gold/…/gold.json`, `Makefile` (`calibrate`) | gold set + runner |
| `ui/` KPI page | interval subtitles/bands; reliability chart; "uncalibrated (n=…)" state |
| `tests/` | Wilson edge cases (n=0, p=0/1); ECE on a hand-computed fixture; mapping application; override round-trip |

## Interview one-liners to have ready

* *"Is the confidence calibrated?"* — "It's an honest heuristic with its
  components stored; calibration is a measured property, so the system
  ships the measurement machinery (reviewer labels, reliability curve,
  ECE) and applies a fitted isotonic mapping once n clears ~100. Until
  then the UI says 'uncalibrated' — implying otherwise would be the bug."
* *"Why min(analyst, critic)?"* — "Two independent estimates of the same
  event; taking the pessimist is the cheap, conservative fusion, and the
  reliability curve will tell us if it's *too* conservative."
* *"Where are the confidence intervals?"* — "On every KPI proportion
  (Wilson, with n shown) and as bootstrap bands on skewed latency/cost
  trends. Per-verdict, the principled version is a conformal prediction
  set with guaranteed coverage — designed, gated on labels."
