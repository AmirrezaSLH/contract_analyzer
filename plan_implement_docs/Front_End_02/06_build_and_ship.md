# Front End 02 · build, ship, and what "done" means

**Status: settled 2026-08-24.** Toolchain, Docker, the commit sequence, and
the acceptance checklist.

## 1. Toolchain

Pin exact versions, not caret ranges. Two of the four load-bearing libraries
have changed an API this plan depends on across a major
(`04_data_layer.md` §4), and a demo is the wrong place to discover it.

```
ui/
  package.json      react, react-dom, react-router, @tanstack/react-query
                    dev: vite, @vitejs/plugin-react, typescript, vitest,
                         openapi-typescript
  tsconfig.json     strict: true, noUncheckedIndexedAccess: true
  vite.config.ts    see below
```

```ts
// vite.config.ts
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: { "/api": { target: "http://localhost:8100", changeOrigin: true } },
  },
  build: {
    outDir: "../src/contract_analyzer/api/static",
    emptyOutDir: true,
  },
});
```

Building into the API package is what lets `StaticFiles` find the bundle with
no copy step and no volume mount. Add that directory to `.gitignore` — the
bundle is a build artefact, not source.

`tsconfig.json` runs `strict`. `src/api/` additionally contains no `any`,
enforced by review rather than a lint rule, because the generated types make
it unnecessary.

## 2. Make targets

| Target | Does |
|---|---|
| `make ui-install` | `npm ci` in `ui/` |
| `make ui-types` | Export `docs/openapi.json` from the app, regenerate `src/api/types.gen.ts` |
| `make ui-dev` | Vite dev server against a locally running API — the fast loop |
| `make ui-build` | `tsc --noEmit && vite build` into the API package |
| `make ui-test` | `vitest run` |
| `make docker-up` | Unchanged name; now serves API and UI on one port |

`make ui-dev` needs the API running separately. `make docker-up` is what the
panel runs, and it must work from a clean clone with no Node installed
locally — which is what the build stage in §3 is for.

## 3. Docker

The runtime image gains a build stage; the `ui` service disappears.

```dockerfile
FROM node:22-slim AS ui-builder
WORKDIR /ui
COPY ui/package*.json ./
RUN npm ci
COPY ui/ ./
RUN npm run build                      # -> ../src/contract_analyzer/api/static

FROM base AS runtime
# ... existing python install ...
COPY --from=ui-builder /src/contract_analyzer/api/static \
     /app/src/contract_analyzer/api/static
```

Adjust the `COPY --from` path to wherever `outDir` resolves inside the build
stage; the point is that the Python image receives a directory of static
files and needs no Node.

In `docker-compose.yml`:

* **Delete the `ui` service** and the `FRONTEND_PORT` mapping.
* The `api` service now serves both, on `BACKEND_PORT` (8100). Its comment about `CA_API_URL`
  and cross-origin requests should be replaced with the reason there is no
  longer a second origin at all.
* `CA_API_URL` remains the **MCP server's** setting. It is no longer the
  UI's.
* Remove the `ui)` case from `docker/entrypoint.sh` in commit 13j, not
  before.

## 4. Commit sequence

| # | Commit | What |
|---|---|---|
| 13a | `feat(api): serve the front end and move routes behind /api` | `APIRouter(prefix="/api")`, `StaticFiles(html=True)` mounted last, `/docs` `/openapi.json` `/health` unmoved; tests updated for the prefix |
| 13b | `feat(ui): vite scaffold, tokens and the api client` | `ui/`, `vite.config.ts`, `tokens.css`, `global.css`, `client.ts`, `errors.ts`, generated types, `make ui-*` |
| 13c | `feat(ui): app shell, sidebar and routing` | `App.tsx`, five routes, sidebar with document scope, trace id, `no_api_key` banner from `/health` |
| 13d | `feat(ui): upload and the document library` | Drop zone with progress, ingest result card, library table, delete dialog. **Blocked on `05_api_deltas.md` §1** |
| 13e | `feat(ui): the analysis view -- submit, poll, report` | All five states, `useAnalysis`, `Disclosure`, `StateChip`, `SubMarker`, `QuoteCard`, `MetricTile`, export. **Includes the gap fixture, §5** |
| 13f | `feat(ui): cited chat with model and retrieval controls` | `sse.ts`, streaming transcript, citation cards, the three settings, Enter-to-send. **Blocked on `05_api_deltas.md` §2** |
| 13g | `feat(ui): error surfaces` | Every code in `01_ui_spec.md` §4 rendered; `errors.test.ts` |
| 13h | `test(ui): the sse reader, the error map and the poll` | `vitest`, split-mid-frame fixtures, the terminal-status assertion |
| 13i | `chore(docker): build the front end into the api image` | Node build stage, compose `ui` service removed |
| 13j | `chore(ui): remove the streamlit front end` | `src/contract_analyzer/ui/`, `tests/test_ui*.py`, `.streamlit/`, the `[ui]` extra, the `ui)` entrypoint case |
| 13k | `docs(ui): the front end, its states and its API calls` | `docs/ui.md`; a row in `architecture.md` |

Two things about the ordering:

**13a is first, and it is an API commit.** Moving every route behind a prefix
is cheap while the only clients are `/docs` and the test suite, and expensive
once the MCP server and a connector point at the old paths. Doing it before
there is a front end to break is the whole point.

**13j is last, and it is a real commit rather than a cleanup.** Two front
ends in one repository is a question the panel will ask and the README cannot
resolve, and a `ui)` entrypoint case that starts an unmaintained Streamlit
app is a live trap during a demo. Delete it once 13e and 13f work — until
then it is the reference for what the API returns and how each error code
reads. Mine `ui/client.py` and `ui/errors.py` on the way past.

Per `AGENTS.md`, anything landing in `plan_implement_docs/` or `tests/` is its
own commit and is not packaged with implementation.

## 5. The gap fixture

**Part of commit 13e, not a follow-up.** The sample contract returns 23/23
met, so every state in `01_ui_spec.md` §5 is unreachable from real data. A
UI verified only against it has not been verified.

`ui/test/fixtures/gaps.json` is an `AnalysisReport` derived from the real
sample report with statuses altered to exercise:

* one `Partially Compliant` and one `Non-Compliant` criterion;
* `partial`, `missing` and `not_determined` sub-requirements;
* at least one quote with `verified: false`;
* one result with `needs_review: true` and a non-empty `unresolved_errors`.

It is clearly labelled as constructed, and it never leaves the test tree —
this is a rendering fixture, not sample output, and it must never be
mistaken for a real run.

A dev-only route or a Vite env flag that renders the Analysis view from the
fixture is worth the ten lines: it is how the gap states get looked at.

## 6. Acceptance

- [ ] `make docker-up` from a clean clone with no Node installed: one
      container serves the UI and the API on `BACKEND_PORT` (8100); `/health` green; `/docs`
      still reachable.
- [ ] A hard refresh on `/documents/1/analysis` returns the app, not a 404.
- [ ] Upload the sample: progress bar advances, then the result card shows a
      document id, 21 pages, 102 chunks, outline from headings.
- [ ] A `.txt` gives `unsupported_media_type` inline; a >25 MB PDF gives
      `payload_too_large`, and nothing is left in `RAW_DIR`.
- [ ] Run an analysis: queued → running with the stage line advancing and
      each criterion filling in as it finishes → done with five rows. **The
      poll stops on the terminal status — confirmed in the network panel.**
- [ ] Every collapsed criterion row shows title, `k of n met`, confidence and
      state chip without being opened.
- [ ] Reload mid-run: same document, same view, poll resumes.
- [ ] Expand a criterion: sub-requirements with full text, two quotes with
      `§` and page, rationale, footer. Export downloads a report that
      validates as `AnalysisReport`.
- [ ] Against the gap fixture: amber and red chips render, `partial` /
      `missing` / `not_determined` markers are distinguishable **in
      greyscale**, and an unverified quote does not look like a verified one.
- [ ] Two documents: an answer in chat on A never cites B, and the sidebar
      always names the document being asked.
- [ ] Chat: text streams token by token, citation cards appear once at the
      end, Enter sends. **Kill the API mid-stream**: the partial answer
      survives, the turn is marked incomplete, no spinner is left running.
- [ ] With `ANTHROPIC_API_KEY` unset: the `no_api_key` banner appears on
      Analysis and Chat, the Run button is disabled with a reason, and upload
      still works.
- [ ] The trace id on the analysis card appears on every line of that run in
      `.run/app.jsonl`; `jq 'select(.trace_id == null)'` over those lines is
      empty.
- [ ] Keyboard only, no mouse: upload, pick a document, open a criterion,
      change a chat setting, send a question, delete a document.
- [ ] `make ui-build` clean with `tsc --noEmit`; `make ui-test` green; no
      `any` in `src/api/`.
- [ ] `ui/` contains no business logic: no PDF handling, no retrieval, no
      prompt, no model name outside the settings allowlist.

## 7. Risks and open questions

| Risk | Mitigation |
|---|---|
| TanStack Query's `refetchInterval` signature moved between majors | Exact pin; the poll has a test |
| SSE framing across chunk boundaries | `sse.ts` pure and tested against split fixtures |
| A dropped stream leaves a spinner | `AbortController` per turn; incomplete is a rendered state |
| The gap states are never seen | Fixture is part of 13e |
| Node in the image bloats the build | Multi-stage; only `dist/` reaches the runtime layer |
| Two front ends confuse the panel | 13j |

**Open questions**, each with a recommendation:

1. **Do the shallow and deep depths have real values?** Not yet — they need a
   recall measurement against the five criteria.
   `{shallow: 3, medium: 6, deep: 12}` is a labelled placeholder.
   *Recommendation:* measure before the demo; it is one script and it makes
   the control defensible when the panel asks.
2. **Does `Depth` belong in front of a customer at all?** It is the one
   control whose options a compliance reviewer cannot reason about.
   *Recommendation:* ship it, watch whether anyone moves it; the alternative
   is fixing it at `medium` and exposing it on the KPI page.
3. **Where does the KPI dashboard live?** Third sidebar entry, fifth route
   (`/metrics`). Its charting library is `KPI_plan.md`'s decision — but it is
   the one place a dependency could reasonably be added, and it should be
   chosen for bundle size.
4. **Re-run history.** `GET /analyses?document_id=` returns every run; the UI
   shows the newest and has no history control. *Recommendation:* defer, but
   never delete an older run.
5. **`/v1` prefix?** `05_api_plan.md` open question 1, now entangled with
   13a's `/api` move. *Recommendation:* still later — `/api/v1` is a one-line
   change on top of what 13a already does.
