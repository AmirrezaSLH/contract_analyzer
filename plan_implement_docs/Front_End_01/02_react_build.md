> **SUPERSEDED by `../Front_End_02/`.** Kept as the record of the
> Streamlit attempt and the reasoning that replaced it. The
> post-mortem is reproduced as §1 of `../Front_End_02/02_architecture.md`;
> nothing here is current.

# Front End 01 · building it in React + TypeScript

**Status: revised 2026-08-24. Supersedes `02_streamlit_build.md`, deleted.**
How to build what `01_ui_spec.md` specifies. Assumes the API of
`05_api_plan.md` **plus** the deltas in `03_api_deltas.md`.

## 0. Why this replaces the Streamlit plan

The Streamlit build was written, works, and looks wrong. It is worth being
precise about why, because two of the three causes are structural rather than
cosmetic — and because the previous revision predicted the cosmetic one and
missed the structural ones.

The implementation was not the problem. `.streamlit/config.toml` carries the
palette, both faces, the radius and a separate `[theme.sidebar]` block;
`ui/theme.py` is three HTML builders and a dozen CSS rules, escaping
everything, exactly as specified. What failed is the fit between the design's
visual system and Streamlit's.

**1. Streamlit has one `secondaryBackgroundColor`, and the design needs two
surfaces.** The design is white cards (`#FFFFFF`, 1px `#E5DDD0`) floating on
warm canvas (`#FAF8F4`); the sidebar is a third, warmer plane (`#F2EEE6`).
Streamlit paints expander headers *and* bodies, `st.chat_message` blocks and
several other surfaces from `secondaryBackgroundColor` — which was set to the
sidebar's beige. The result is that the card system never appears: every
criterion row and every chat turn renders as a beige slab, and the page
becomes two warm greys three percent apart with no figure and no ground. The
quote card is the only white surface in the product, because it is the only
one built by hand.

*This one may be a one-line experiment rather than a rewrite:* set
`secondaryBackgroundColor = "#FFFFFF"` at root and leave the beige under
`[theme.sidebar]`, which exists for exactly this. It is worth five minutes
before abandoning the branch, and it does not change the conclusion below.

**2. The collapsed criterion row lost half its content.** `st.expander`'s
label is one markdown string, so `k of n met` and `conf 0.95` were demoted to
a caption *inside* the expander — invisible while collapsed. The collapsed
list of five criteria is the primary scanning surface of the entire product:
it is where a reviewer decides which criterion to open. Two of its four data
points are not on it. The state also travels as `:green-badge[…]`, which is
Streamlit's badge rather than the design's chip, so `state_chip()` — which
exists and is correct — goes unused on the one screen that most needs it.
The previous revision called this a loss of *alignment*. It is a loss of
*information*.

**3. Vertical rhythm, as predicted.** Streamlit's inter-element gaps plus
`st.write("")` and `st.divider()` as spacers cannot produce the 22px-between,
10px-within cadence, and every `st.markdown` label carries its own paragraph
margin. This is the cosmetic one, and on its own it would have been
acceptable.

There is a fourth, smaller: the metric row is four different objects — one
hand-built label-plus-chip beside three `st.metric` calls — which cannot
align with each other and are not cards at all.

Three things follow, and they are why this is a smaller job than it sounds:

1. **The specification does not change.** `01_ui_spec.md` was written as a
   design document, not a Streamlit document. Tokens, states, screens and
   copy all carry over unaltered. This document replaces one chapter, not
   the book.
2. **The prototype is already the implementation.** `design/Main.dc.html` is
   HTML with inline styles and a small state object. Porting it to TSX is
   mechanical — extract components, lift the inline styles into CSS Modules,
   replace the local state object with hooks. You are not redrawing anything.
3. **Two modules port almost directly.** `ui/client.py` and `ui/errors.py`
   are the API surface and the error-code table; they become `api/client.ts`
   and the error rendering, with the same shapes and the same decisions.
   `ui/theme.py`'s three builders become three components. The Streamlit
   branch is not wasted work — it is a first draft of the client layer.

What you take on instead: a build toolchain, a second language, and the
browser as a first-class client, which makes CORS and browser auth real
questions. §2 and `03_api_deltas.md` §5 answer both — the short version is
that a Vite proxy in development and a static mount in production mean the
browser only ever talks to one origin, so neither becomes a problem.

## 1. Decisions

1. **Vite + React + TypeScript. Not Next.js.** There is no SSR requirement,
   no server-side rendering budget, and no reason to put a Node runtime in
   the production image. Vite builds a static bundle; FastAPI serves it.
2. **The built bundle is served by FastAPI.** `app.mount("/", StaticFiles(...))`
   at the end of the route table. One container, one port, one URL, and the
   browser never makes a cross-origin request — so `api_cors_origins` stays
   empty, exactly as it is today. In development, Vite's `server.proxy`
   forwards `/api/*` to `localhost:8100`, so the browser sees one origin
   there too. **CORS is never configured, in either mode.** This is a
   deliberate choice and the reason it is worth stating twice.
3. **TanStack Query owns server state.** The analysis job is a polled
   resource, and `refetchInterval` with a predicate that stops on a terminal
   status is precisely the tool for it. It also gives cache invalidation on
   upload and delete, which is the whole of the library view's freshness
   logic. Component state (`useState`) owns UI state only: which criterion is
   open, the three chat settings, the draft question.
4. **No global state library.** There is no state that TanStack Query and a
   dozen `useState` calls do not cover. Redux and Zustand would both be
   ceremony over a four-screen app.
5. **CSS Modules over design tokens. No Tailwind, no component library.**
   `01_ui_spec.md` §2 already enumerates the token set; it becomes one
   `tokens.css` of custom properties, and every component references
   `var(--…)`. Tailwind would require re-expressing the same tokens in a
   config and would put utility soup between you and values you have already
   settled. A component library (MUI, shadcn) would need overriding
   everywhere, because the design is bespoke rather than a re-skin.
   *The one exception to consider later:* if keyboard and focus behaviour on
   the dropdowns and the delete dialog turn out to be fiddly, adopt Radix
   primitives **for those two components only**.
6. **Types are generated from the OpenAPI document, never hand-written.**
   `docs/openapi.json` is already a required deliverable (assignment §3.3).
   `openapi-typescript` turns it into `src/api/types.gen.ts`, checked in and
   regenerated by a `make` target. The API schema stays the single source of
   truth for response shapes, and a backend change that breaks the UI becomes
   a type error rather than a runtime surprise.
7. **The URL carries the scope.** `/upload`, `/library`,
   `/documents/:id/analysis`, `/documents/:id/chat`. React Router, four
   routes. This is a genuine improvement over the previous plan, not a
   port artefact: a panel member can bookmark a finished analysis, and the
   "which document am I looking at" question is answered by the address bar
   as well as the sidebar.

## 2. Toolchain and layout

The front end lives at the repository root as `ui/`, not under
`src/contract_analyzer/` — it is not a Python package and must not look like
one.

```
ui/
  package.json  tsconfig.json  vite.config.ts  index.html
  src/
    main.tsx                React Router, QueryClientProvider
    App.tsx                 layout: <Sidebar/> + <Outlet/>
    api/
      client.ts             fetch wrapper: base URL, X-Trace-Id, ApiError
      types.gen.ts          generated; do not edit
      sse.ts                POST + ReadableStream SSE reader (§5)
    hooks/
      useDocuments.ts  useDocument.ts  useAnalysis.ts  useChat.ts
    styles/
      tokens.css            01_ui_spec.md §2, verbatim
      global.css            resets, font imports, base type
    components/
      StateChip  SubMarker  QuoteCard  MetricTile  Select  Tooltip
      Avatar  Icon  Button  ProgressBar  EmptyState
    views/
      Upload/  Library/  Analysis/  Chat/
```

`vite.config.ts` needs two things and nothing else:

```ts
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": { target: "http://localhost:8100", changeOrigin: true } } },
  build: { outDir: "../src/contract_analyzer/api/static", emptyOutDir: true },
});
```

Building into the API package is what lets `StaticFiles` find the bundle with
no copy step and no volume mount. Add that directory to `.gitignore`; the
bundle is a build artefact.

**Base path.** The client calls `/api/...`. In development Vite proxies it; in
production FastAPI serves the API under the same prefix. That means the API's
routes move behind `/api` — one `APIRouter(prefix="/api")`, and the
`/v1` question in `05_api_plan.md` open question 1 is untouched by it.

## 3. Tokens

`tokens.css` is a transcription of `01_ui_spec.md` §2, one custom property per
row of those tables. It is the only file in the front end that contains a
literal colour:

```css
:root {
  --canvas: #FAF8F4;      --sidebar: #F2EEE6;     --surface: #FFFFFF;
  --surface-sel: #FBF8F2; --nav-sel: #E5DCCC;
  --border: #E0D9CC;      --border-card: #E5DDD0; --border-ctl: #D6CDBD;
  --rule-quote: #C8A88C;
  --ink: #23201B;   --ink-body: #3A342B;  --ink-2: #4A443A;
  --muted: #6E665A; --meta: #7C7365;      --label: #9A9082;
  --accent: #7A3B2E; --accent-hover: #5C2B21; --on-accent: #FDF9F3;
  --fc-fg: #2F6B4F; --fc-bg: #EEF5F0; --fc-br: #B9D3C4;
  --pc-fg: #8A6108; --pc-bg: #FBF3E3; --pc-br: #E4D0A6;
  --nc-fg: #8F2E2E; --nc-bg: #FAEDEC; --nc-br: #E3BFBB;
  --serif: "Source Serif 4", Georgia, serif;
  --sans: "Source Sans 3", system-ui, sans-serif;
}
```

A component that hardcodes a hex is a bug. The accent alternates in the spec
mean the whole palette pivots on `--accent` alone.

## 4. Analysis: submit, poll, render

The polled job is the one piece of real machinery in the app.

```ts
export function useAnalysis(analysisId: string | null) {
  return useQuery({
    queryKey: ["analysis", analysisId],
    queryFn: () => api.getAnalysis(analysisId!),
    enabled: analysisId !== null,
    refetchInterval: (q) => {
      const s = q.state.data?.status;
      return s === "queued" || s === "running" ? 2000 : false;
    },
  });
}
```

`refetchInterval` returning `false` on a terminal status is what stops the
poll; there is no cleanup to get wrong and no interval to leak. Confirm the
predicate signature against the TanStack Query major you pin — it changed
between v4 and v5 — and pin it in `package.json` rather than a caret range.

Two behaviours the view must get right:

* **A `200` from `POST /analyses` is a success, not an error.** It means the
  duplicate-submit guard matched an in-flight run; take `analysis_id` from
  the body exactly as you would from a `202`.
* **`status: "failed"` carries a real `error` string** from the runner.
  Render it. A generic "something went wrong" throws away the one piece of
  information the screen has.

The four states of `01_ui_spec.md` §3.3 are one component switching on
`status` plus whether a report exists — the same card mutating, never a
different layout, so nothing jumps as a run progresses.

## 5. Chat: streaming over POST

**`EventSource` cannot be used.** It is GET-only and cannot set headers, and
`/chat` is a POST carrying a JSON body. The client is `fetch` plus a
`ReadableStream` reader and about forty lines of SSE framing:

```ts
const res = await fetch("/api/chat", { method: "POST", headers, body, signal });
const reader = res.body!.pipeThrough(new TextDecoderStream()).getReader();
// accumulate into a buffer, split on "\n\n", parse "event:" and "data:" lines,
// dispatch: text -> append delta, tool_call -> update the working line,
// citations -> store once, done -> usage and cost, error -> terminate cleanly
```

Three things `EventSource` would have given you for free and now must be
written:

1. **Reconnection.** There is none. A dropped stream is a failed turn; keep
   the partial text, mark the turn incomplete, and offer a retry. Never leave
   a bare spinner — that is the one failure mode a live demo cannot survive.
2. **Cancellation.** Hold an `AbortController` per turn and abort it on
   unmount and on a new question, or a backgrounded stream keeps appending
   into a component that is gone.
3. **Buffer discipline.** SSE frames split across chunk boundaries. Parse
   from an accumulating buffer, never per chunk.

A `503 no_api_key` arrives *before* the stream opens, as an ordinary JSON
error response. Check `res.ok` before touching `res.body`, render the `hint`,
and do not append a turn.

## 6. Depth

The one place the UI deliberately hides a parameter. `Depth` maps to
`retrieval_top_k`, hardcoded in the client, never shown:

```ts
export const DEPTH_TOP_K = { shallow: 3, medium: 6, deep: 12 } as const;
```

`medium` must equal `settings.retrieval_top_k` (6), so the default UI choice
and the default backend behaviour are the same thing. The shallow and deep
values are a starting point — set them from a real measurement of recall
against the five criteria and record the measurement here.

## 7. Components worth naming

Most of the design is ordinary markup. Four components carry the design's
identity and should be built first, because everything else composes them:

| Component | Contract | Notes |
|---|---|---|
| `StateChip` | `state: ComplianceState` | The three-colour table in the spec, driven by a class per state — never a colour prop |
| `SubMarker` | `status: SubRequirementStatus` | Four treatments including the **dashed** `not_determined`, which must not read as "absent" |
| `QuoteCard` | `quote: ResolvedQuote` | Serif text, 3px `--rule-quote` left rule, `§ ref · p. N · verified`. One card renders both the analysis quote and the chat citation — which is why `03_api_deltas.md` §3 asks the two endpoints to agree on field names |
| `Select` | value, options, `help?` | Label, chevron, menu, and the hover tooltip Retrieval and Depth carry |

`QuoteCard` is the component the product is about. Build it first and get it
right.

**Escaping is no longer a manual concern.** React escapes interpolated text
by default, so the previous plan's warning about hand-built HTML strings does
not carry over — but the corollary does: **never** reach for
`dangerouslySetInnerHTML`. Quote text is extracted from a PDF a user
uploaded. There is no reason for the design to need raw HTML anywhere.

## 8. Trace ids

`api/client.ts` mints one `X-Trace-Id` per user-initiated action — an upload,
an analysis submission, a question — not per HTTP request. It is stored
alongside the analysis and displayed on its card. That is what makes the live
log walkthrough of assignment §3.6 work: a panel member reads an id off the
screen and greps `.run/app.jsonl` for it.

## 9. Docker

The runtime image gains a build stage; the `ui` service disappears.

```dockerfile
FROM node:22-slim AS ui-builder
WORKDIR /ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build            # -> /ui/dist

FROM base AS runtime
# ...
COPY --from=ui-builder /ui/dist /app/src/contract_analyzer/api/static
```

In `docker-compose.yml`, delete the `ui` service and its `FRONTEND_PORT` mapping.
The API service now serves both the API and the front end on `BACKEND_PORT` (8100), and its
comment about `CA_API_URL` and cross-origin requests should be replaced by
the reason there is no longer a second origin at all. `CA_API_URL` remains
the **MCP server's** setting; it is no longer the UI's.

`make dev-ui` runs Vite against a locally running API for the fast loop;
`make docker-up` serves the built bundle. Both are worth having — the second
is what the panel runs.

## 10. Commit sequence

| # | Commit | What |
|---|---|---|
| 13a | `feat(ui): vite scaffold, tokens and the api client` | `ui/`, `vite.config.ts`, `tokens.css`, `global.css`, `client.ts`, generated types, `make ui-types` |
| 13b | `feat(api): serve the built front end and move routes behind /api` | `APIRouter(prefix="/api")`, `StaticFiles` mount, SPA fallback for client-side routes |
| 13c | `feat(ui): app shell, sidebar and routing` | `App.tsx`, four routes, the sidebar with document scope and trace id |
| 13d | `feat(ui): upload and the document library` | Drop zone, ingest result card, library table, delete with confirmation |
| 13e | `feat(ui): the analysis view -- submit, poll, report` | All four states, `useAnalysis`, criterion rows, `StateChip`, `SubMarker`, `QuoteCard`, export |
| 13f | `feat(ui): cited chat with model and retrieval controls` | `sse.ts`, transcript, citation cards, the three settings, Enter-to-send |
| 13g | `feat(ui): error surfaces` | Every `code` in the API's error table gets a rendering; blocked on `01_ui_spec.md` §5 item 1 |
| 13h | `chore(docker): build the front end into the api image` | Node build stage, static mount, compose `ui` service removed |
| 13i | `docs(ui): the front end, its states and its API calls` | `docs/ui.md`; a row in `architecture.md` |
| 13j | `chore(ui): remove the streamlit front end` | `src/contract_analyzer/ui/`, `tests/test_ui*.py`, `.streamlit/`, the `[ui]` extra, the `ui)` case in `docker/entrypoint.sh` |

13b lands early on purpose: doing the prefix move and the static mount before
there is a front end to break is far cheaper than after.

**13j is last, and it is a real commit rather than a cleanup.** Two front
ends in one repository is a question the panel will ask and an ambiguity the
README cannot resolve — and a `ui)` case in the entrypoint that starts a
Streamlit app nobody maintains is a live trap during a demo. Delete it once
13e and 13f are working, not before: until then it is the reference for what
the API returns and how each error code reads. Mine `ui/client.py` and
`ui/errors.py` on the way past (§0), then remove the directory whole.

`01_ui_spec.md` §5 items 2 and 3 — the unverified quote and the
partial/non-compliant result — are **design** work that must land before 13e
is called done, or the analysis view will only ever have been seen against a
contract that passes everything.

## 11. Acceptance

- [ ] `make docker-up`; one container serves the UI and the API on `BACKEND_PORT` (8100), and
      `/health` is green. No `ui` service, no second port.
- [ ] Upload the sample PDF: the result card shows a document id, 21 pages,
      102 chunks, outline from headings.
- [ ] A `.txt` gives the `unsupported_media_type` message; a >25 MB PDF gives
      `payload_too_large`, and nothing is left in `RAW_DIR`.
- [ ] Run an analysis: queued → running with the stage line advancing and
      each criterion filling in as it finishes → done with five rows. The
      poll stops on the terminal status — confirmed in the network panel,
      not assumed.
- [ ] Reload the page mid-run: the URL restores the same document and view,
      and the poll resumes.
- [ ] Expand a criterion: sub-requirements, two quotes with `§` and page,
      rationale, footer. Export downloads a report that validates as
      `AnalysisReport`.
- [ ] Two documents: an answer in chat on A never cites B, and the sidebar
      always names the document being asked.
- [ ] Chat: text streams token by token, one set of citation cards appears at
      the end, Enter sends. Kill the API mid-stream: the partial answer
      survives, the turn is marked incomplete, no spinner is left running.
- [ ] The trace id shown on the analysis card appears on every line of that
      run in `.run/app.jsonl`.
- [ ] `npm run build` clean with `tsc --noEmit`; no `any` in `src/api/`.
- [ ] `ui/` contains no business logic: no PDF handling, no retrieval, no
      prompt, no model name outside the settings selector's allowlist.

## 12. Open questions

1. **Does `Depth` belong in front of a customer at all?** It is the one
   control whose options a compliance reviewer cannot reason about. The
   alternative is to fix it at `medium` and expose it only on the KPI page.
   Recommendation: ship it, watch whether anyone moves it.
2. **Where does the KPI dashboard live?** A third sidebar entry and a fifth
   route (`/metrics`), per `01_ui_spec.md` §1. Its charting library is
   `KPI_plan.md`'s decision, not this document's — but it is the one place a
   dependency could reasonably be added, and it should be chosen for bundle
   size.
3. **Re-run history.** `GET /analyses?document_id=` returns every run; the
   UI shows the newest and has no history control. Recommendation: defer,
   but do not delete older runs.
4. **Should the SPA fall back to the API's `/docs`?** The FastAPI Swagger UI
   is the §3.3 connector artefact and must stay reachable once the SPA owns
   `/`. Mount the static files last, and keep `/docs`, `/openapi.json` and
   `/api/*` ahead of the catch-all.
