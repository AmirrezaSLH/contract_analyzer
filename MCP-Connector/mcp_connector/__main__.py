"""`python -m mcp_connector`.

`python -m mcp_connector.server` runs the same thing, and is the spelling
`docker/entrypoint.sh` has used since before this package existed.
"""

from __future__ import annotations

from .server import main

if __name__ == "__main__":
    raise SystemExit(main())
