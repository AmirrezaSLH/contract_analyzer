# The front end

Four surfaces — upload, library, analysis, chat — over the HTTP API, and
nothing else. The design is `plan_implement_docs/Front_End_01/`; this file is
what was built and why it is shaped the way it is.

**The rule that shapes everything here: the UI holds no logic.** It parses no
PDF, opens no database and calls no model. It makes HTTP requests to
`CA_API_URL` and renders what comes back. That is `docs/api.md`'s rule — *the
API contains no logic the CLI does not have* — extended one hop: if a handler
is tempted into the UI it belongs in the API, and if it is not in the API it
belongs in the library first. `ui/` imports nothing from `api/`.

## Running it

```bash
pip install -e ".[ui]"
make api           # in one terminal
make ui            # in another; http://localhost:8501
# or
make docker-up     # both, on 8000 and 8501
```

`CA_API_URL` is the whole of its configuration (`http://localhost:8000` by
default, `http://api:8000` under compose). `API_KEY` is picked up from the
environment if the API demands one.

## The shape

| | |
|---|---|
| **The sidebar is application navigation** | Upload, the library, the document list, the active document, the trace id. Picking a contract here sets the scope. |
| **The tabs are views of that document** | **Analysis** and **Chat**, rendered only once something is in scope. Upload and Library have no tab row — they are not views of a document. |
| **Everything is scoped to one contract** | Not a UI convention: `retrieve()`, `chat()` and `analyze_document()` all take a `document_id`, and nothing on screen may come from another. |
| **The server is the state** | The ids are. This holds view state, a per-document transcript and three control values; a reload loses none of the work. |

The KPI dashboard, when it lands, is a **third sidebar entry** — application
level, spanning every document — not a fifth tab.

## Module layout

```
ui/
  app.py       set_page_config, the sidebar, the view switch
  client.py    ApiClient over httpx2: one method per endpoint, one ApiError,
               one place X-Trace-Id is minted and X-API-Key attached
  state.py     every session_state key with its default, in one dict
  theme.py     the tokens, and the three HTML builders
  layout.py    the page header, and escape()
  errors.py    every API `code` -> a headline, the hint, the trace id
  views/       upload.py  library.py  analysis.py  chat.py
```

## Decisions worth defending

### `st.segmented_control`, not `st.tabs`

Not a style preference. `st.tabs` cannot be switched programmatically, and the
design requires exactly that: the library's **Analyse** and **Chat** buttons
put the reviewer on a specific view of a specific document, and the upload
result card does the same. A segmented control is backed by `session_state`, so
any button can set it.

`st.tabs` also executes **every tab body on every re-run**, which would render
the chat transcript on every two-second analysis poll.

### The poll is a fragment; nothing here needs SSE

`@st.fragment(run_every="2s")` re-runs the status block and nothing else.
Without it a two-second poll re-runs the whole script for three minutes:
re-fetching the document list, re-rendering the other tab, re-drawing the
report as it arrives.

The `criteria: [{id, status, state?, confidence?}]` array on
`GET /analyses/{id}` is exactly the progress table the running view draws — so
the API's decision to leave streaming and cancellation per-process costs this
UI nothing. **Polling is the contract.**

### One request per render, not one per row

The sidebar and the library both draw from a single `GET /documents`. That is
what `last_analysis`, `pages` and `chunks` were added to that endpoint for: a
Streamlit script re-runs on every click, so a lookup per row is a lookup per
row *per click*. The sidebar reads the active document out of the same list
rather than calling `GET /documents/{id}` again.

`last_analysis.states` is a count per compliance state, and the sentence — "5
of 5 compliant", "2 gaps found" — is composed here. The API does not choose
this client's words.

### Raw HTML in three places, and nowhere else

The state chip, the sub-requirement marker and the quote card. Each is
something Streamlit has no primitive for:

* the **chip** must carry its words as well as its colour — state in colour
  alone is state a colourblind reviewer cannot read, and it is the most
  important thing on the screen;
* the **marker**'s *shape* is the distinction: `not_determined` is a dashed
  outline because "we could not tell" must not read as "we checked and it is
  absent";
* the **quote card**'s 3px left rule cannot come from
  `st.container(border=True)`, which draws four uniform sides.

**Everything interpolated is escaped.** A quote is text extracted from a PDF
somebody uploaded and a filename is whatever the client put in a multipart
header. `html.escape` is applied at the boundary in `theme.py` and
`layout.escape`, and colours and statuses are looked up in tables rather than
interpolated, so a value the API invents cannot become CSS.

### `app.py` imports absolutely, and `header` is not in it

`streamlit run src/contract_analyzer/ui/app.py` executes that file as
`__main__` with **no package context**, so a relative import there raises
`ImportError: attempted relative import with no known parent package` the
moment a browser connects — a failure that does not show up until a session
exists, which is why it is written down. `app.py` uses absolute imports; the
view modules keep relative ones, because they *are* imported as package
modules.

For the same reason the shared page header lives in `layout.py`. A view doing
`from ..app import header` would import `contract_analyzer.ui.app` as a second
module object and re-execute the script body, `main()` included, inside the
first one.

### Depth is an abstraction, and the number never reaches the screen

`Depth` maps to `retrieval_top_k`:

```python
DEPTH_TOP_K = {"shallow": 3, "medium": 6, "deep": 12}
```

This is the one place the UI knowingly hides a parameter. A compliance reviewer
has no basis for choosing 4 passages over 8, but does have a basis for choosing
"deep" when a clause is buried in an exhibit. `medium` is **set from
`/health`'s `retrieval_top_k` at boot**, so leaving the control alone and
having no control at all are the same thing. The other two are a starting point
and should be re-set from a real recall measurement.

### Nothing about the backend is hardcoded

`GET /health` is called once per session and supplies the model list (which is
the same allowlist `POST /chat` validates against, so the picker cannot offer a
choice the API will refuse), the retrieval defaults, the upload cap and the
pool shape. `GET /criteria` supplies the criterion titles — a progress row
carries an id and nothing else, and `data_in_transit` is not a name to put in
front of a reviewer.

`key_present` is why the **Run compliance analysis** button is *disabled* with
a tooltip rather than clickable and refused: that field exists so a UI can grey
a button out instead of spending a click to discover a 503.

### Error surfaces

`errors.py` maps every `code` in the API's table to a headline, then renders
the API's `message` and its `hint` beneath it — which is what the copy pass on
`hint` was for: it is read by a person here, not only by a model recovering
from a tool call. Two codes are minted client-side, because they describe
something that happened on this side of the wire: `unreachable` and
`bad_response`.

A failure inside a dialog or just before an `st.rerun()` has nowhere to draw
itself — the run it happened in is over — so it is stashed in `session_state`
and rendered at the top of the next one. `errors.guard` catches `ApiError` and
nothing else: anything else is a bug in this UI and should reach Streamlit's
handler with its traceback intact.

### Chat holds the transcript, keyed by document

The API is stateless, so the list in `session_state` *is* the conversation, and
it is sent back on every question. It is keyed by `document_id` because
carrying one contract's transcript onto another is exactly the leak the product
is built to prevent. Capped at 50 turns — `chat()` only replays the last 8, so
the cap is about this process's memory, not the model's window.

Citations are stored **with the turn**, so re-rendering the transcript costs
nothing. Two failures are handled apart: a `503` before the stream opens
appends no turn at all (a question with no reply is worse than no question),
and an `error` event mid-stream keeps the partial text and marks the turn
incomplete — never a bare spinner.

## Version pin

Four APIs are version-sensitive. All four exist at the pin this was built and
tested against, **streamlit 1.62.0**, so none of the fallbacks the build plan
allowed for are in the code:

| API | Fallback if a pin loses it |
|---|---|
| `st.segmented_control` | `st.radio(horizontal=True)` |
| `st.fragment(run_every=…)` | a manual `st.rerun()` loop with a `time.sleep` |
| `[theme.fontFaces]` / `theme.baseRadius` | a CSS `@import`, and radius in `theme.py` |

`pyproject.toml` pins `streamlit>=1.45`, the first release carrying all four.

## Testing

`tests/test_ui.py` drives the app with `streamlit.testing.v1.AppTest`, which
runs `app.py` the way a browser session does — the whole script, on every
interaction. That is the only kind of test that means anything here: the
interesting failures are *re-run* failures (a key that exists on the first pass
and not the second, a widget default fighting `session_state`, a view rendered
with a scope that just changed), and none are reachable by calling a render
function directly.

The API is a stub that records what it was asked for, which is how the scoping
assertions work: the proof that chat cannot leak across documents is that the
request carried the right `document_id`. The wire itself is `test_api.py`'s.

## What is not here yet

* **Citation → source.** A quote card names its section and page but does not
  open the passage. `GET /documents/{id}/sections` exists for it.
* **Streaming tool trail.** `box.tool_calls` is collected and not displayed;
  the working line the design shows ("searching … — hybrid retrieval") is not
  drawn.
* **Analysis history.** `GET /analyses?document_id=` returns every run; this
  shows the newest and has no history control. Re-run keeps the older one.
* **The KPI dashboard.** `KPI_plan.md`. Third sidebar entry.
* **Responsive behaviour below ~1100px.** The design is specified at 1440; the
  sidebar and the two-column sub-requirement grid break first.
