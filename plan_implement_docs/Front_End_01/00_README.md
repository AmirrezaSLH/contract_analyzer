> **SUPERSEDED by `../Front_End_02/`.** Kept as the record of the
> Streamlit attempt and the reasoning that replaced it. The
> post-mortem is reproduced as §1 of `../Front_End_02/02_architecture.md`;
> nothing here is current.

# Front End 01 — the customer-facing UI

**Status: design settled 2026-08-24; front-end stack revised to React +
TypeScript the same day, after the Streamlit build was tried and rejected
(`02_react_build.md` §0).** Covers the four customer-facing
surfaces only — upload, library, analysis, chat. The KPI dashboard is
`KPI_plan.md`'s and is deliberately absent here; it lands as a fifth
surface later, and `01_ui_spec.md` reserves its place in the navigation.

The design exists as a clickable prototype, not a picture:

* **Canvas** — https://claude.ai/code/artifact/62a2c14d-2d14-488b-ae86-192f8f1ef454
  (page **App** is the design; page **Directions** holds the two rejected
  directions, kept as a record of what was decided against and why)
* **Source** — `design/Main.dc.html` in this repo, plus `design/canvas.json`.
  It is a self-contained HTML file with the real analysis output baked in;
  open it to read exact values rather than eyeballing the render.

Read in this order:

| # | Document | What it settles |
|---|---|---|
| 1 | [`01_ui_spec.md`](01_ui_spec.md) | What the UI is: navigation, tokens, every screen and state, copy |
| 2 | [`02_react_build.md`](02_react_build.md) | How to build it: stack, tokens, module layout, polling, streaming, Docker, commits |
| 3 | [`03_api_deltas.md`](03_api_deltas.md) | What `05_api_plan.md` must gain before this UI is buildable |

**Read `03_api_deltas.md` first if you are the one implementing the API.**
Two of the four surfaces need endpoint changes that do not exist in the API
plan today, and one of them (chat settings) is a schema change. Building the
UI against the current contract means building three placeholders.

## The one-paragraph summary

One document at a time. The sidebar is application navigation — upload,
library, and the list of documents — and picking a document there sets the
scope for everything else. The two tabs, **Analysis** and **Chat**, are views
*of the selected document*, so they only appear once a document is in scope.
Analysis is a job: submit, poll, read the report. Chat is a stream. Every
answer either carries a verbatim quote with its section and page, or says it
could not find one. Nothing on screen is ever sourced from a document other
than the one in the sidebar.
