"""The HTTP API: one backend, four consumers.

The React UI, the MCP server, an external connector and the CLIs all reach
the same functions. This package is the HTTP surface over them and nothing
more: every handler is `parse the request -> call a library function -> shape
the response`, and anything a handler would otherwise decide for itself lives a
layer down where `scripts/analyze.py` can reach it too.

    api/
      main.py      create_app, the lifespan, the trace middleware
      deps.py      what a handler is given: settings, embedder, runner, conn
      errors.py    one error envelope, and everything that maps onto it
      schemas.py   the wire types (library models reused, not mirrored)
      uploads.py   client bytes onto disk, safely
      jobs.py      analyses as background jobs
      sse.py       event framing and the fan-out behind both streams
      routes/      one module per resource

See docs/api.md.
"""

from .main import create_app

__all__ = ["create_app"]
