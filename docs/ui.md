# The front end

Four surfaces — upload, library, analysis, chat — as a Vite + React app in
`ui/` at the repository root. It is not a Python package. The design is
`plan_implement_docs/Front_End_02/`; this file is what was built and why it is
shaped the way it is.

**The rule that shapes everything here: the UI holds no logic.** It parses no
PDF, opens no database and calls no model. It talks to `/api` and renders what
comes back. That is `docs/api.md`'s rule — *the API contains no logic the CLI
does not have* — extended one hop: if a handler is tempted into the UI it
belongs in the API, and if it is not in the API it belongs in the library
first. `ui/` imports nothing from the Python package.

**One origin.** In production FastAPI serves the built bundle at `/` and the
JSON API under `/api`. In development Vite proxies `/api` at the API. There
is no configurable base URL in the browser bundle, and `api_cors_origins`
stays empty because the browser never makes a cross-origin request.

## Running it

```bash
./start.bash              # API on BACKEND_PORT (8100); UI at /
./start.bash --dev        # plus Vite on FRONTEND_PORT (8101), proxying /api
make docker-up            # same as the first: one port, one origin
```

`make api` after `make ui-build` is the same as a plain `./start.bash`. A
fresh clone with no bundle still starts the API; `/` is absent until the
bundle exists. Docker builds the bundle in a Node stage and copies it into
the Python image, so `make docker-up` does not need Node on the host.

## The shape

| | |
|---|---|
| **The sidebar is application navigation** | Upload, the library, the document list, the active document, the trace id. Picking a contract here sets the scope. |
| **The tabs are views of that document** | **Analysis** and **Chat**, rendered only once something is in scope. Upload and Library have no tab row — they are not views of a document. |
| **The URL is the scope** | `:id` is the single source of truth. The sidebar reads it; nothing writes a copy of it into React state. |
| **Everything is scoped to one contract** | Not a UI convention: `retrieve()`, `chat()` and `analyze_document()` all take a `document_id`, and nothing on screen may come from another. |
| **The server is the state** | The ids are. A reload loses none of the work. Chat's transcript is the exception: the API is stateless, so the list in memory *is* the conversation. |

The KPI dashboard, when it lands, is a **third sidebar entry** — application
level, spanning every document — not a fifth tab.

Routes: `/upload`, `/library`, `/documents/:id/analysis`,
`/documents/:id/chat`. `/_gaps` exists in development only, so every analysis
state in the spec can be looked at against a sample that is otherwise
all-green.

## Module layout

```
ui/
  src/api/         client.ts, errors.ts, sse.ts, types.gen.ts
  src/hooks/       TanStack Query: documents, analysis (poll), chat, health
  src/components/  chips, quote cards, error surfaces, the shell pieces
  src/views/       Upload, Library, Analysis, Chat
  src/styles/      tokens.css, global.css
```

`make ui-types` regenerates `types.gen.ts` from `docs/openapi.json`. Views
call hooks; hooks call `client.ts`; nothing else in `ui/` mentions `fetch`.

## Decisions worth defending

### The poll is a query; chat is SSE

Analysis is a job: submit, poll `GET /analyses/{id}` until a terminal status,
read the report. TanStack Query's `refetchInterval` with a terminal-status
predicate is that machine. The `criteria: [{id, status, state?, confidence?}]`
array on that endpoint is exactly the progress table the running view draws.
**Polling is the contract.**

Chat is a stream. `sse.ts` splits frames so a token that arrives split across
TCP packets is still one event. Buffering that stream would look like a hung
request.

### Depth is an abstraction, and the number never reaches the screen

`topKFor(depth, configured)` maps shallow / medium / deep onto
`retrieval_top_k`. **`medium` is `/health`'s `retrieval_top_k`**, so leaving
the control alone and having no control at all are the same thing. Shallow is
half, deep is double, clamped to the API's 1..20. The ratios are a labelled
placeholder pending a recall measurement.

### Nothing about the backend is hardcoded

`GET /health` supplies the model list (the same allowlist `POST /chat`
validates against), the retrieval defaults, the upload cap and whether a key
is present. `GET /criteria` supplies the titles — a progress row carries an
id and nothing else.

`key_present` is why **Run compliance analysis** is *disabled* with a tooltip
rather than clickable and refused.

### Error surfaces

`errors.ts` maps every API `code` to a placement (`inline`, `replaces-card`,
`banner`, `full-pane`) and a title. Views switch on the *surface*, never on
the code. There is no generic toast. Two codes are minted client-side:
`unreachable` and `bad_response`. An unknown code falls back to an inline
error rather than a white screen.

### One request per list, not one per row

The sidebar and the library both draw from a single `GET /documents`. That is
what `last_analysis`, `pages` and `chunks` were added to that endpoint for.
`last_analysis.states` is a count per compliance state; the sentence — "5 of
5 compliant", "2 gaps found" — is composed here.

## Testing

Python: `tests/test_ui_serving.py` pins that the API serves the bundle, that
`/api` and `/` never collide, and that CORS stays empty. It uses a miniature
bundle in `tmp_path` so the suite does not depend on `make ui-build`.

TypeScript: `make ui-test` (`vitest`) covers the SSE reader, the error map
and the depth mapping.

## What is not here yet

* **Citation → source.** A quote card names its section and page but does not
  open the passage. `GET /documents/{id}/sections` exists for it.
* **Streaming tool trail.** Tool calls are collected and not displayed.
* **Analysis history.** `GET /analyses?document_id=` returns every run; this
  shows the newest.
* **The KPI dashboard.** `KPI_plan.md`. Third sidebar entry.
* **Responsive behaviour below ~1100px.** The design is specified at 1440.
