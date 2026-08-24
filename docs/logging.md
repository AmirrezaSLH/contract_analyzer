# Logging

`src/contract_analyzer/logger.py` is the only logging surface in the project.
Every module starts with

```python
from ..logger import get_logger
log = get_logger(__name__)
```

and `tests/test_logger.py::test_no_module_imports_logging_directly` fails the
build if any file under `src/contract_analyzer/` imports `logging` itself.

## Why one module

The assignment requires structured logs with trace/span ids that let a panel
reconstruct a request end to end. Doing that with per-module `logging`
configuration means every module has to agree on a format and pass ids
through every function signature. Centralising it means:

* one **format** -- console gets a compact human line, the file gets JSON;
* one **context** -- `trace_id`, `span_id`, `parent_span_id` live in
  `contextvars`, so a request sets them once and every line below it carries
  them, including lines written by the HTTP retry layer and, later, the agents;
* one **place to change** when the sink becomes OpenTelemetry or a log
  shipper -- the call sites do not move.

## API

| Function | What it does |
|---|---|
| `get_logger(name)` | a `logging.Logger` under the `contract_analyzer.` root namespace |
| `configure_logging(level, json_file, console=True, force=False)` | installs the console (stderr) and JSON-file handlers; idempotent so a script and the library it imports cannot double-print; raises third-party loggers (`httpx`, `anthropic`, `openai`) to WARNING |
| `trace_context(trace_id=None)` | context manager binding a trace id; mints a 32-hex id when none is given; nested blocks inherit the outer id |
| `span(name, **attrs)` | context manager logging `span.start` and `span.end` with `latency_ms`, `status` (`ok`/`error`), and the attribute bag the block filled in; sets `span_id` and `parent_span_id` for everything inside |
| `current_trace_id()`, `current_span_id()`, `new_id()` | helpers |

Usage:

```python
with trace_context() as tid, span("ingest.file", path=str(path)) as s:
    parsed = parse_pdf(path)
    s["pages"] = parsed.page_count
    log.info("parsed", extra={"elements": len(parsed.elements)})
```

produces, in `.run/app.jsonl`:

```json
{"ts":"2026-08-23T18:04:11.201+00:00","level":"INFO","logger":"contract_analyzer.span","msg":"span.start","trace_id":"3f…","span_id":"9a…","parent_span_id":null,"span":"ingest.file","path":"data/samples/Sample Contract.pdf"}
{"ts":"…","level":"INFO","logger":"contract_analyzer.ingest.pipeline","msg":"parsed","trace_id":"3f…","span_id":"9a…","parent_span_id":null,"elements":145}
{"ts":"…","level":"INFO","logger":"contract_analyzer.span","msg":"span.end","trace_id":"3f…","span_id":"9a…","parent_span_id":null,"span":"ingest.file","path":"…","pages":21,"status":"ok","latency_ms":812.4}
```

and on the console:

```
18:04:11 INFO  span span.start span=ingest.file path=data/samples/… trace=3f1c9b20
18:04:12 INFO  ingest.pipeline parsed elements=145 trace=3f1c9b20
18:04:12 INFO  span span.end span=ingest.file pages=21 status=ok latency_ms=812.4 trace=3f1c9b20
```

## Conventions

* **`extra={...}` is the idiom** for structured fields; string-formatting them
  into the message defeats the JSON sink. Python reserves a few record
  attribute names (`filename`, `module`, `name`, `msg`, `args`) -- use
  `file`/`path` instead of `filename`.
* **Spans are nouns with a dot**: `parse.pdf`, `ingest.file`, `retrieve`,
  `llm.call`, `agent.router`. The metrics store keys on these names -- a
  renamed span is a renamed row, and `spans WHERE name = 'chat'` is where
  chat cost lives.
* The context filter sits on the **handlers**, not the logger: a logger's
  filters only see records logged directly to it, and every record here
  arrives by propagation from a child logger.
* Levels: `INFO` for pipeline milestones and spans, `WARNING` for retries and
  degraded paths (fake embedder, missing outline), `ERROR` for exhausted
  retries and failed documents. `DEBUG` is for per-element parser detail.

## The metrics store subscribes here

`metrics/` attaches a `logging.Handler` to this root logger that turns every
`span.end` record into a row in `spans` -- so the KPI page is fed by the same
records `.run/app.jsonl` holds, and **no call site changed**. That is why
`Handler` and `LogRecord` are re-exported from this module: exactly one file
in the package may import `logging`, and the dependency runs `metrics ->
logger`, never the other way. See [metrics.md](metrics.md).

`run_id` is carried alongside `trace_id` for the same handler's benefit: one
trace legitimately contains an upload *and* an analysis, so a waterfall needs
to know which spans belong to the run.

## What is deliberately not here yet

OpenTelemetry export and sampling. Spans are stored in full: one analysis is
about seventy rows, and a sampling knob nobody tunes is a knob set wrong during
a demo.

## Tests

`tests/test_logger.py`: JSON line carries extras and the trace id; nested
`trace_context` inherits; `span` logs start/end with latency and parent
linkage; errors are recorded and re-raised; the "no direct `logging` import"
guard.
