"""The MCP surface of the Contract Analyzer, as its own package.

`MCP-Connector/` holds everything about this surface -- the server, the HTTP
client it reaches the API with, the tool schemas, its tests and its
documentation -- and imports nothing from `contract_analyzer`. That is a
boundary, not a filing decision: the connector is a *client* of the API, the
same way the React app is, and a package that could import the analyzer would
sooner or later read the database directly and turn `CA_API_URL` into a
suggestion.

    python -m mcp_connector                      # stdio, for a local desktop client
    python -m mcp_connector --transport http     # streamable HTTP on MCP_PORT

See `docs/mcp.md` for the tool set, the transports and the rationale behind
both.
"""

from __future__ import annotations

from .client import ApiClient, ApiFailure
from .config import ConnectorSettings, get_settings
from .server import INSTRUCTIONS, build_server, main

__all__ = [
    "INSTRUCTIONS",
    "ApiClient",
    "ApiFailure",
    "ConnectorSettings",
    "build_server",
    "get_settings",
    "main",
]
