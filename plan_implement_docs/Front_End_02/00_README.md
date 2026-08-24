# Front End 02 — the customer-facing UI, in React

**Status: settled 2026-08-24. Supersedes `Front_End_01/` entirely.** That set
was written for Streamlit, then patched for React; this one is written for
React from the ground up and is self-contained. `Front_End_01/` stays in the
tree as the record of what was tried and why it was abandoned — its
post-mortem is reproduced here as §1 of `02_architecture.md`, so nothing is
lost by not reading it.

Scope: the four customer-facing surfaces — **upload, library, analysis,
chat**. The KPI dashboard is `KPI_plan.md`'s and is deliberately absent; it
lands as a third sidebar entry and a fifth route later, and `01_ui_spec.md`
reserves its place.

## The design is a running artefact, not a picture

* **Canvas** — https://claude.ai/code/artifact/62a2c14d-2d14-488b-ae86-192f8f1ef454
  Page **App** is the design. Page **Directions** holds the two rejected
  visual directions.
* **Source** — `design/Main.dc.html`, a self-contained HTML file with the
  real analysis output baked in. It is the reference implementation of the
  markup: read exact values there rather than eyeballing the render.

Where this plan and the prototype disagree about a *value*, the prototype
wins. Where they disagree about *behaviour*, this plan wins — the prototype
has no error states, no streaming text and no gap states, and those are
specified here.

## Read order

| # | Document | Settles |
|---|---|---|
| 1 | [`01_ui_spec.md`](01_ui_spec.md) | What the UI is: navigation, tokens, every screen, every state, error surfaces, copy |
| 2 | [`02_architecture.md`](02_architecture.md) | Why React, the stack, how it is served, repo layout, routing, what owns which state |
| 3 | [`03_components.md`](03_components.md) | The component inventory: one contract per component, and which prototype block each comes from |
| 4 | [`04_data_layer.md`](04_data_layer.md) | The API client, generated types, query keys, the polling machine, the SSE reader, error mapping, trace ids |
| 5 | [`05_api_deltas.md`](05_api_deltas.md) | What `05_api_plan.md` must gain. **Read first if you own the API** |
| 6 | [`06_build_and_ship.md`](06_build_and_ship.md) | Toolchain, Docker, make targets, commit sequence, acceptance, risks |

## The one-paragraph summary

One document at a time. The sidebar is application navigation — upload,
library, and the document list — and picking a document there sets the scope
for everything else. The two tabs, **Analysis** and **Chat**, are views *of
the selected document*, and the URL carries both. Analysis is a job: submit,
poll, read the report. Chat is a stream. Every answer either carries a
verbatim quote with its section and page, or says it could not find one.
Nothing on screen is ever sourced from a document other than the one in the
sidebar — which is a library invariant, not a UI convention, and the UI makes
it visible rather than hiding it.

## What is decided, and what is not

**Decided.** The visual design, in full. The stack: Vite, React, TypeScript,
TanStack Query, React Router, CSS Modules over design tokens, no component
library. The serving model: FastAPI serves the built bundle, routes move
behind `/api`, and CORS is never configured. Types are generated from
`docs/openapi.json`.

**Not decided, and flagged where it matters.** The shallow/deep values behind
the **Depth** control (`01_ui_spec.md` §3.4, needs a recall measurement).
Whether `Depth` belongs in front of a customer at all. What the KPI page's
charting library will be. Re-run history. Each is an open question at the end
of the document that owns it, with a recommendation.

**Blocked on someone else.** Five API changes, in `05_api_deltas.md`. Two of
them block a surface outright; one is how the front end is served at all and
should land before anything else in this plan.
