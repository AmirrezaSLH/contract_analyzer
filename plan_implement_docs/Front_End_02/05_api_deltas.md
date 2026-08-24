# Front End 01 · what the API must gain

**Status: raised 2026-08-24.** A change request against `05_api_plan.md`,
written from the UI in `01_ui_spec.md`. Five items. **Item 5 is how the front
end is served at all and should land before anything else in this plan**; two
block a surface; two are cheap corrections that will cost more later than
now.

Nothing here reverses a decision in the API plan. Each shape below extends a
response that already exists, and each exists because a screen cannot be
drawn without it.

## 1. `GET /documents` is too thin for the library — **blocks 13d**

**Today:** `[{document_id, filename}]`, newest first.

**The library table needs** page count, chunk count, when it was added, and
the outcome of its last analysis. With the current shape the UI has two
options, and both are bad: call `GET /documents/{id}` once per row (N+1 on
every mount of the library route, and again on every cache invalidation), or
drop three columns from the design.

**Proposed:**

```json
[
  {
    "document_id": 1,
    "filename": "Sample Contract.pdf",
    "pages": 21,
    "chunks": 102,
    "created_at": "2026-08-24T04:30:50Z",
    "last_analysis": {
      "analysis_id": "17",
      "status": "done",
      "completed_at": "2026-08-24T11:08:12Z",
      "states": {"Fully Compliant": 5, "Partially Compliant": 0, "Non-Compliant": 0},
      "needs_review": 0
    }
  }
]
```

`last_analysis` is `null` when there is none — that is what drives the "Not
analysed" chip and the empty state in the Analysis view. `states` is a count
per state, not a summary string: the UI composes "5 of 5 compliant" or "2
gaps found" in its own words, and a different consumer will want different
words.

`pages` and `chunks` are already on the `documents` row and in the `chunks`
table; `list_documents(conn)` in prerequisite 3 of the API plan is where the
join belongs.

**Cost:** one query in `db.py`, one model field. It is smaller now than after
`GET /documents` has three consumers.

## 2. `POST /chat` cannot carry the chat settings — **blocks 13f**

**Today:** `ChatRequest` is `{document_id, question, history?, stream?}`.

The chat view exposes three controls — Model, Retrieval, Depth — and there is
no field for any of them. Built against the current schema, all three are
decoration.

**Proposed** — three optional fields, defaulting to the configured values:

| Field | Type | Default | Validation |
|---|---|---|---|
| `model` | string | `settings.answer_model` | **Allowlist**, not free text |
| `retrieval_mode` | `"hybrid" \| "vector" \| "keyword"` | `settings.retrieval_mode` | The existing `RetrievalMode` literal (`config.py:38`) |
| `top_k` | int | `settings.retrieval_top_k` | Clamp to 1–20 |

Three notes on the model field, in order of importance:

* **It must be an allowlist.** `POST /chat` is open when `API_KEY` is unset,
  and a free-text `model` on an open endpoint is a request to spend money on
  an arbitrary model. The allowlist is the three ids the UI offers.
* The chosen model must appear in the `done` event's payload and in the
  `AnswerResult`, so the usage line can report what actually answered rather
  than what was asked for.
* `chat()` takes its client from `get_client(settings)`; per-request model
  selection means threading the id through to the call, not building a second
  client.

`top_k` is what the UI's **Depth** maps onto. The mapping is the frontend's
and stays there (`01_ui_spec.md` §3.4) — the API sees a number.

## 3. One citation, two field names — **cheap, and cheapest now**

The API plan projects chat citations as
`{evidence_id, quote, title, page_display, chunk_id, start, end}`, while
`ResolvedQuote` in the analysis report carries
`{text, evidence_id, section_ref, page_display, chunk_id, verified}`.

The same fact — *which clause this came from* — is `title` in one response
and `section_ref` in the other, and the quote itself is `quote` in one and
`text` in the other. The UI renders both with the same card
(`theme.quote_card`), so it would carry a translation layer whose only
purpose is to paper over a naming accident.

**Proposed:** the chat citation uses `section_ref` and `text`, matching
`ResolvedQuote`, and adds `verified`. Then one card renders both, and the
"verified" marker means the same thing on both screens.

## 4. `hint` is read by humans too

The error envelope's `hint` is described as "the sentence a model can act on"
— *"call GET /documents to list document_id and filename"*. The UI puts it in
front of a person: it is the second line of every error surface.

Both audiences are served by one sentence in plain language that names the
action, not the endpoint. *"Upload a contract first, or pick one from the
library"* works for the model and the reviewer; the endpoint spelling works
only for the model.

**Proposed:** no schema change. Write `hint` for a person, and let the `code`
carry the machine-readable half — which it already does.

## 5. The API serves the front end, and its routes move behind `/api`

**New with the React front end, and it replaces a container rather than
adding one.**

A browser client makes two questions real that the previous plan did not have
to answer — which origin the front end is served from, and how it
authenticates. Both dissolve if the answer to the first is "the same one":

* **Production.** Vite builds a static bundle into
  `src/contract_analyzer/api/static/`, and the app mounts it:
  `app.mount("/", StaticFiles(directory=..., html=True))`, **last**, after
  every API route. One container, one port, one URL, no CORS, and the
  "single setup script to run in localhost" the assignment asks for stays a
  single script.
* **Development.** Vite's `server.proxy` forwards `/api/*` to
  `localhost:8100`. The browser sees one origin there too.

For that to work the API's routes move behind an `/api` prefix — one
`APIRouter(prefix="/api")` in `main.py`. Three things must stay ahead of the
static mount and keep their current paths: `/docs`, `/openapi.json` (the
§3.3 connector artefact, and the source of the front end's generated types)
and `/health` (the Docker healthcheck already targets it).

`html=True` on the mount is what makes client-side routes work: a hard
refresh on `/documents/1/analysis` must return `index.html`, not a `404`.

**Authentication is unchanged and stays unchanged.** `X-API-Key` in a browser
bundle is public, so it is not a secret and must not be treated as one. For
the local demo `API_KEY` is unset and the API is open, exactly as
`05_api_plan.md` decision 8 has it; production is OAuth 2.1 with per-tenant
`document_id` scoping, exactly as that decision already says. The React
client changes nothing here — it only makes it obvious why the current model
is a demo model, which is worth saying out loud in the walkthrough.

## What does not need to change

Recorded so it is not re-litigated:

* **SSE for analyses stays cut.** The UI polls `GET /analyses/{id}` every two
  seconds through TanStack Query's `refetchInterval`, which stops itself on a
  terminal status, and the `criteria: [{id, status, state?, confidence?}]`
  array is exactly what the running view draws. The hardest thing in the API
  plan buys this UI nothing.
* **`POST /chat`'s stream needs no GET twin.** `EventSource` cannot POST, but
  `fetch` plus a `ReadableStream` reader can, and that is what the client
  does. Adding a GET variant of `/chat` to satisfy `EventSource` would put a
  question and its history in a URL, which is worse in every respect.
* **`Quotes verified` and `Needs review` tiles** are computed client-side by
  walking `report.results` — `ResolvedQuote.verified` and
  `ComplianceResult.needs_review` are already there. If `totals` later grows
  these counts, the UI should prefer them, but nothing is blocked.
* **The `Overall` tile** is the worst state across the five results,
  computed client-side. It is a presentation decision, not a backend fact.
* **CORS.** `api_cors_origins` stays empty — still, and now deliberately.
  The browser is the client, but it never sees a second origin: in
  development Vite proxies `/api/*` to `localhost:8100`, and in production
  FastAPI serves the built bundle itself (item 5). Configuring CORS would be
  a symptom of having got the serving story wrong.
* **`POST /analyses` returning `200` with an in-flight analysis** is correct
  and the UI handles it as a success. Do not turn the duplicate-submit guard
  into an error.

## Order

| Item | Blocks | Size |
|---|---|---|
| 1 · `GET /documents` fields | Library (13d) | one query, one model |
| 2 · chat settings on `POST /chat` | Chat controls (13f) | three fields, one allowlist, one plumbing change |
| 3 · citation field names | Nothing; costs a shim if deferred | rename |
| 4 · human `hint` copy | Error surfaces (13g) | copy pass |
| 5 · `/api` prefix and static mount | **Everything** (13a) | one router prefix, one mount, one Dockerfile stage |

Item 5 first, and early: moving every route behind a prefix is cheap while
the only clients are `/docs` and the test suite, and expensive once the MCP
server and a connector are pointing at the old paths. Items 1 and 2 should
land in the same week as 12d–12f. Item 3 should land before any client
depends on `title`.
