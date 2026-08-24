# Front End 02 · architecture

**Status: settled 2026-08-24.** Why React, what the stack is, how it is
served, and what owns which piece of state.

## 1. Why not Streamlit — the post-mortem

A complete Streamlit front end was built (`src/contract_analyzer/ui/`, ~2,400
lines) and abandoned. It works and it looks wrong. Recording why, because two
of the three causes are structural and one of them is a lesson that applies
to the React build too.

The implementation was not at fault. `.streamlit/config.toml` carried the
palette, both faces, the radius and a separate `[theme.sidebar]` block;
`ui/theme.py` was three HTML builders and a dozen CSS rules, escaping
everything. It followed its plan closely.

**1. One `secondaryBackgroundColor`, two surfaces needed.** The design is
white cards on warm canvas with the sidebar as a third, warmer plane
(`01_ui_spec.md` §2.2, "the two-surface rule"). Streamlit paints expander
headers *and* bodies, `st.chat_message` blocks and several other surfaces
from a single `secondaryBackgroundColor`, which was set to the sidebar beige.
The card system therefore never appeared: every criterion row and every chat
turn rendered as a beige slab, and the page became two warm greys three
percent apart with no figure and no ground. The quote card was the only white
surface in the product, because it was the only one built by hand.

**2. The collapsed criterion row lost half its content.** `st.expander`'s
label is a single markdown string, so `k of n met` and `conf 0.95` were
demoted to a caption *inside* the expander — invisible while collapsed. The
collapsed list of five is where a reviewer decides what to open. Two of its
four data points were not on it. The state also travelled as a Streamlit
badge rather than the design's chip, so the correct `state_chip()` builder
went unused on the screen that most needed it.

The Front_End_01 plan had predicted this and called it a loss of *alignment*.
It is a loss of *information*, and misranking it as cosmetic is why the build
was attempted at all.

**3. Vertical rhythm.** Streamlit's inter-element gaps plus `st.write("")`
and `st.divider()` as spacers cannot produce a 22px-between / 10px-within
cadence, and every markdown label carries its own paragraph margin. On its
own this would have been acceptable.

A fourth, smaller: the metric row was one hand-built tile beside three
`st.metric` calls, which cannot align and are not cards.

**The lesson that carries forward.** Causes 1 and 2 are both the framework's
component model refusing a structure the design depends on. React has no such
model, which removes the class of problem — but it also removes the excuse.
`01_ui_spec.md` §2.2 and §3.3d now state the two-surface rule and the
four-data-point row as *rules* rather than as description, because they are
the two things that were lost.

**The branch is not wasted.** `ui/client.py` and `ui/errors.py` are the API
surface and the error-code table; they port almost directly to
`api/client.ts` and the error rendering, with the same shapes and the same
decisions. Mine them before deleting the directory (`06_build_and_ship.md`,
commit 13j).

## 2. Stack

| Layer | Choice | Why |
|---|---|---|
| Build | **Vite** | Static bundle, no Node in the production image, one config file |
| UI | **React + TypeScript** | The design is a component tree; the API has a schema |
| Server state | **TanStack Query** | The analysis is a polled resource, and `refetchInterval` with a terminal-status predicate is exactly that. Also gives cache invalidation on upload and delete |
| Routing | **React Router** | Four routes; the URL carries the scope |
| Styling | **CSS Modules over CSS custom properties** | `01_ui_spec.md` §2 becomes one `tokens.css`; every component reads `var(--…)` |
| Types | **`openapi-typescript`** against `docs/openapi.json` | The API schema stays the single source of truth |
| Tests | **Vitest** | For the three pieces with real logic: SSE framing, error mapping, depth mapping |

**Not Next.js.** No SSR requirement, no server-rendering budget, and no
reason to put a Node runtime in the production image.

**No Tailwind.** The token set is already enumerated and settled. Tailwind
would require re-expressing it in a config and put utility soup between the
implementer and values that are already decided.

**No component library.** MUI or shadcn would need overriding everywhere,
because this design is bespoke rather than a re-skin. *One exception to
revisit:* if keyboard and focus behaviour on the three dropdowns and the
delete dialog turns out fiddly, adopt Radix primitives **for those two
components only** — that is a real accessibility argument, not a styling one.

**No global state library.** TanStack Query plus a dozen `useState` calls
cover everything. Redux or Zustand would be ceremony over a four-screen app.

## 3. How it is served

The single decision that makes CORS and browser auth disappear: **the browser
only ever talks to one origin.**

* **Production.** Vite builds into `src/contract_analyzer/api/static/`, and
  FastAPI mounts it — `app.mount("/", StaticFiles(directory=…, html=True))`,
  **last**, after every API route. One container, one port, one URL. The
  "single setup script to run in localhost" the assignment asks for stays a
  single script.
* **Development.** Vite's `server.proxy` forwards `/api/*` to
  `localhost:8100`. Same origin from the browser's point of view.

Consequences, all of them good:

* **`api_cors_origins` stays empty**, in both modes. Configuring CORS would
  be a symptom of having got this wrong.
* **The API's routes move behind `/api`** — one `APIRouter(prefix="/api")`.
  `/docs`, `/openapi.json` and `/health` keep their paths and stay ahead of
  the static mount. `html=True` is what makes a hard refresh on
  `/documents/1/analysis` return `index.html` rather than a 404.
* **Authentication is unchanged.** `X-API-Key` in a browser bundle is not a
  secret and must not be treated as one. For the local demo `API_KEY` is
  unset and the API is open, exactly as `05_api_plan.md` decision 8 has it;
  production is OAuth 2.1 with per-tenant `document_id` scoping, exactly as
  that decision already says. The React client changes nothing here — it
  only makes it obvious *why* the current model is a demo model, which is
  worth saying out loud in the walkthrough.

See `05_api_deltas.md` §5 for the API-side work.

## 4. Repository layout

The front end lives at the repository root as `ui/`. It is not a Python
package and must not look like one.

```
ui/
  package.json  tsconfig.json  vite.config.ts  index.html
  src/
    main.tsx                  router + QueryClientProvider
    App.tsx                   <Sidebar/> + <Outlet/>
    api/
      client.ts               fetch wrapper: base URL, X-Trace-Id, ApiError
      types.gen.ts            generated; never edited
      sse.ts                  POST + ReadableStream SSE reader
      errors.ts               code -> surface + copy (01_ui_spec.md §4)
    hooks/
      useDocuments.ts  useDocument.ts  useUpload.ts
      useAnalysis.ts   useCreateAnalysis.ts  useChat.ts
    styles/
      tokens.css              01_ui_spec.md §2, verbatim
      global.css              reset, font imports, base type
    components/               03_components.md
    views/
      Upload/  Library/  Analysis/  Chat/
  test/
    sse.test.ts  errors.test.ts  depth.test.ts
```

**The no-logic rule, one hop further out than the API's.** `05_api_plan.md`
says "the API contains no logic that the CLI does not have". The UI contains
no logic that the API does not have: it parses no PDF, opens no database,
calls no model, and knows no prompt. The one deliberate exception is
presentational computation — the Overall tile's worst-of-five, the two quote
counts, and the words for the library's "last analysis" chip — which are
presentation decisions rather than backend facts, and are documented as such
where they occur.

## 5. Routing

| Route | View | Notes |
|---|---|---|
| `/` | redirect | to `/library` if any document exists, else `/upload` |
| `/upload` | Upload | No tab bar |
| `/library` | Library | No tab bar |
| `/documents/:id/analysis` | Analysis | Tab bar; `:id` is the scope |
| `/documents/:id/chat` | Chat | Tab bar; `:id` is the scope |
| `*` | Not found | Back to library |

`:id` is the single source of truth for scope — the sidebar reads it, not the
other way round. A document id that 404s renders `document_not_found` as a
full-pane empty state (`01_ui_spec.md` §4), never a crash.

## 6. What owns which state

| State | Owner | Notes |
|---|---|---|
| The active document id | **The URL** | Not React state. Copyable, linkable, reload-safe |
| Document list, one document, analysis status, criteria | **TanStack Query** | Server state. Never mirrored into `useState` |
| `analysis_id` per document | **Query cache**, derived from `last_analysis` | Not a local map; that is a second source of truth |
| Which criterion is open | `useState` in the Analysis view | Resets on document change, which is correct |
| The three chat settings | `useState` in the Chat view, lifted to a small context if a second consumer appears | Session-scoped, not per document |
| The chat transcript | `useState` in the Chat view | The API is stateless; this is the client's `history`. Capped at 50 turns in memory; `chat()` caps what is sent at 8 messages |
| The draft question | Uncontrolled input + ref | No re-render per keystroke |
| Trace id per action | `api/client.ts`, stored beside the analysis | One per user action, never per request |

**The rule behind the table:** anything the server knows lives in the query
cache and is read from there; anything only this browser tab knows lives in
component state; anything a user should be able to send to a colleague lives
in the URL.

## 7. Risks

| Risk | Mitigation |
|---|---|
| TanStack Query's `refetchInterval` predicate signature changed between v4 and v5 | Pin an exact version; the poll has a test that asserts it stops on a terminal status |
| SSE framing across chunk boundaries is easy to get subtly wrong | `sse.ts` is pure and unit-tested against split-mid-frame fixtures |
| A dropped chat stream leaves a spinner forever | `AbortController` per turn; an incomplete turn is a rendered state, not an absence (`01_ui_spec.md` §4 rule 3) |
| The gap states are never seen because the sample is all-green | A fixture is part of commit 13e, not a follow-up |
| Two front ends in the repo confuse the panel | Commit 13j deletes the Streamlit one, after 13e and 13f work |
