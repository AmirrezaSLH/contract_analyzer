# Step 14 — the MCP server: a data plane, not a second agent

**Status: implemented, 2026-08-24**, on branch `mcp-connector` (15 commits).
The code is `MCP-Connector/`; the documentation is
[`docs/mcp.md`](../docs/mcp.md). This file is the plan it was built from, kept
as the record of what was decided and — in [What changed](#what-changed-while-building-it)
— what the plan got wrong.

The host (Claude Desktop, Copilot, any MCP client) is already an agent. MCP
is a **data plane** over the same FastAPI the UI uses: ids, jobs, and
retrieved passages. Generation the product must stand behind stays on the
analysis pipeline. Generation the host already does (free-form chat) stays
on the host.

Assignment §3.3 needs a diagram, a tool schema, and a written rationale
(auth, request/response, state, multi-turn). A working FastMCP server is
the skeleton; a full Claude Desktop integration is not required. All four
artifacts are in `docs/mcp.md`.

## Role split (the decision that shapes the tool list)

| Job | Who generates | MCP surface |
|---|---|---|
| The five Table-1 criteria | **Our** router / extractor / evaluator | `analyze_compliance` then `get_analysis` |
| Follow-up questions | The **host** | `search_contract` (passages only) |
| Conversational chat | The React UI + `POST /chat` | **not an MCP tool** |

**Analyze, do not let the host score compliance.** A host that reads
passages and fills Table 1 will invent quotes, skip the evaluator, and
produce a report that never appears as an `analysis_id` in logs or KPIs.
Nested LLM is acceptable here: analysis is a 60–180 s job with a schema,
not a conversation the host is already having.

**Retrieve, do not wrap chat.** `POST /chat` exists for the UI: evidence
ledger, citation verification, `no_context` when the ledger is empty. On
MCP it becomes the host calling a second model, with `history` the host
will not replay well, and with streaming dropped (`stream=false`). The
host already owns the transcript. Give it `document_id`-scoped passages
and let it talk. Do not register both `ask_contract` and
`search_contract` — the host will pick at random.

Internal agent tools (`search_contract` / `get_section` inside a
criterion run) stay behind `POST /analyses`. MCP does not re-expose the
router or the evaluator.

## Tool set

Server `instructions` (connect-time) tell the host the protocol. Tools
are the live operations. Shipped as `server.INSTRUCTIONS`:

```text
1. Call get_started. If key_present is false, tell the user; do not start analysis.
2. If there is no document_id, call upload_contract (path or url) or list_contracts.
3. Table 1 / "is this contract compliant" → analyze_compliance, then poll get_analysis.
   Use detail=summary until the user asks for quotes or rationale.
4. Any other question about the same PDF → search_contract(document_id, query).
   Answer only from the passages. If they are empty, say the contract does not contain it.
5. Pass document_id / analysis_id on every call. This server has no session.
```

| Tool | HTTP | Notes |
|---|---|---|
| `get_started` | `GET /health` + `GET /criteria` | Live state: `key_present`, `auth_required`, `documents`, `analyses_running`, and a `next_step` sentence. Not a static README. |
| `list_criteria` | `GET /criteria` | The five questions with their sub-requirements — the parts a verdict is built from. |
| `upload_contract` | `POST /documents` | `path` **or** `url`. Never base64. |
| `list_contracts` | `GET /documents` | Recovers ids after reconnect. Carries `last_analysis`, so a host can offer an existing report. |
| `analyze_compliance` | `POST /analyses` | **Start only.** Returns `{analysis_id, status, poll_after_seconds}` immediately. Optional `criterion_ids`. |
| `get_analysis` | `GET /analyses/{id}?detail=` | Poll. Default `detail="summary"` (state, confidence, progress). `full` on request. `retry_after_seconds` until terminal. |
| `search_contract` | **new** `POST /documents/{id}/search` | Query-scoped passages: `{section, breadcrumb, page_display, text, chunk_id}`. `top_k` capped in the connector (hybrid, keyword fallback). No `mode` / `top_k` knobs. |

Optional later, not in the first cut: `get_section(document_id, prefix)`
over `retrieve_by_section` if "open Exhibit G" is worth a second retrieval
tool. Skip it if `search_contract` is enough for the demo. **Skipped.**

Return Pydantic models from every tool so FastMCP emits `outputSchema`.
Annotations: `readOnlyHint` on getters and search; `openWorldHint` on
upload-by-url. Both asserted in `MCP-Connector/tests/`.

### Not tools

| HTTP | Why |
|---|---|
| `POST /chat` | Host generates; see above. Bonus chat stays on the UI. |
| `GET /analyses/{id}/events` | MCP is request/response. Poll. |
| `POST /analyses/{id}/cancel` | Host stops polling. |
| `DELETE /documents/{id}` | A confused model will wipe the demo corpus. |
| `GET /documents/{id}/sections` | UI section picker. |
| `/metrics/*` | Dashboard, not the host. |

### Upload

Tool arguments are JSON in the host context. A 326 KB PDF as base64 is
~435 KB and is billed on every retry. So:

- `path` — stdio only; the server reads the file. **Refused on the HTTP
  transport**, where it would read the server's own filesystem on behalf of
  whoever can reach the port. `MCP_UPLOAD_ROOT` narrows it further.
- `url` — the connector downloads it, checking the size while it streams and
  the `%PDF` magic before forwarding.
- Human upload — through the web UI; the host then calls `list_contracts`.

Each `POST /documents` **mints a new `document_id` even for identical
bytes** (isolation between sessions). Said in the tool description and
repeated in its result, so the host does not assume "same file = same id."

### State and multi-turn (assignment rationale)

The server is stateless. `document_id` and `analysis_id` are the handles.
The host's conversation is the transcript; we do not store one. That is
the same contract as the REST API.

## Transport and packaging

FastMCP, Python, thin **httpx client over `CA_API_URL`**, never SQLite.
One code path for jobs, auth, and metrics.

`MCP_TRANSPORT=stdio|http`, and the same server object serves either.
Compose runs HTTP on `MCP_PORT` (8102), `mcp` service off the `x-app`
anchor with `volumes: []`, `CA_API_URL=http://api:${BACKEND_PORT}`,
`depends_on: api` on `service_healthy`. `./start.bash` runs the same thing
locally, passing the transport and API URL as flags. Stdio stays for a local
Claude Desktop demo. A desktop client that only speaks stdio reaches HTTP
through `mcp-remote`; the demo should not discover that live.

## API delta

There is no retrieve route today. Add a small one so MCP does not grow a
second data path:

`POST /documents/{id}/search` — `{query, top_k?}` → a short list of passages
from `retrieve(..., document_id=id)`. Same isolation invariant as chat
and analysis. Documented in `docs/api.md` and in the OpenAPI export.

MCP does not call `retrieve()` itself.

**A second delta the plan missed:** `X-Surface` on `POST /analyses`. See
[What changed](#what-changed-while-building-it).

## Observability, errors, auth

- Mint a trace id per tool call, send `X-Trace-Id`, tag the run
  `surface="mcp"` so the KPI dashboard can slice API vs UI vs MCP.
- Tool failures: the API envelope (`code`, `message`, `hint`) as
  `isError`. Never a traceback. `document_not_found` + "call
  `list_contracts`" is what lets the host recover.
- Demo auth: `X-API-Key` from MCP to the API — and in a local checkout
  `API_KEY` is unset, so there is **no authentication at all**. Production:
  OAuth 2.1, MCP server as resource server, role-based access control across
  the seven tools (they are not equally consequential: `search_contract`
  reads, `analyze_compliance` spends a dollar), `document_id` scoped per
  tenant, and a per-subject spend limit. Isolation today is enforced;
  ownership is not.

## Testing

Offline: `list_tools()` (names, schemas, annotations) plus a mock
transport standing in for the API, in the style of the `http_client`
tests. No network, no keys.

**Plus a second suite the plan did not call for**, and should have:
`test_against_the_api.py` drives the same tools through a real `create_app()`
in-process. A fake API keeps agreeing with a connector that has quietly
stopped working; this is what catches a renamed field on the day it is
renamed. 52 connector tests; 444 in the suite.

## Assignment artifacts

All in [`docs/mcp.md`](../docs/mcp.md):

1. Diagram: chat client → MCP → FastAPI → ingest / jobs / retrieve /
   analyze. No DB arrow from MCP.
2. Tool schemas from FastMCP (`outputSchema` + annotations) plus the
   OpenAPI export (18 operations) as the connector spec.
3. Rationale: static API key vs OAuth and RBAC; ids as state; analyze is a
   job; host chat vs our analysis; search instead of nested `POST /chat`.
4. The URL a client connects to, including why claude.ai cannot reach a
   `localhost` connector without a tunnel.

## What changed while building it

Seven things. Five are the plan being wrong, and they are worth keeping
written down.

1. **`wait_seconds ≤ 30` on `analyze_compliance` was incoherent and is gone.**
   Our own measurement puts a run at 60–180 s, so a 30 s wait could never
   return a finished analysis — it only holds a connection open. Replaced by
   `poll_after_seconds` on the start call and `retry_after_seconds` on every
   unfinished poll, from `mcp_poll_seconds`. A host left to its own instincts
   either asks twice a second or asks once, sees `running`, and reports
   failure.

2. **`surface="mcp"` had no mechanism.** `surface` was hardcoded at the call
   site (`api/jobs.py`, `"api"`), so the KPI slice the plan promised could not
   have worked — and worse, the browser and a third-party connector were
   already both `"api"`, so the axis did not exist for any of the three. Added
   `X-Surface` on `POST /analyses`, an allowlist of
   `api | ui | mcp | connector`, stored on the `analyses` row. An unknown value
   is `422` rather than a silent fall back: a run filed in a bucket its caller
   does not believe it is in makes a split nobody can reproduce.

3. **`path` uploads are refused over HTTP.** The plan noted `path` was for
   stdio but did not say to *reject* it elsewhere. Unenforced, a host over
   HTTP names a path and the server reads its own filesystem — inside a
   container, where the host's paths mean something else — for whoever can
   reach the port. Now refused with a hint naming `url`, plus an optional
   `MCP_UPLOAD_ROOT`.

4. **Port 8765 → `MCP_PORT` (8102).** The repo moved off hardcoded ports in
   `39ba0c6`; 8102 was already in `.env`.

5. **The connector is its own package, `MCP-Connector/mcp_connector/`, not
   `contract_analyzer.mcp`.** It imports nothing from the analyzer, which is
   what keeps `CA_API_URL` honest: a package that could import `config.py`
   would eventually open the database and answer locally while claiming to
   talk to a remote API. A test asserts the absence of the import.
   `docker/entrypoint.sh` runs `python -m mcp_connector`.

6. **Upload-by-url uses the connector's own httpx client, not the analyzer's
   retrying one.** Same reason as (5): `http_client.py` exists for calls to
   model providers over the internet, and importing it would drag `config.py`
   in. No retries anywhere in the connector — the two operations worth
   retrying, upload and analysis, are the two where a blind retry pays twice.

7. **`.env` carries only `MCP_PORT`.** `MCP_TRANSPORT`, `CA_API_URL`,
   `MCP_HOST` and `MCP_UPLOAD_ROOT` are defaults in code, still read from the
   process environment. A port is a fact about a machine; a transport is a
   decision the launcher has already made, and both launchers (`start.bash`
   by flag, compose by service environment) make it.

Two smaller ones: a passage's text is `text`, not `quote` — a "quote" in this
API is an extraction the model API pulled from a passage we sent, with a
`verified` flag, and this carries no such guarantee. And the
`contract://criteria` resource was skipped; client support for resources is
uneven and `list_criteria` works everywhere.
