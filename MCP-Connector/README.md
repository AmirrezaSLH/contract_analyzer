# MCP-Connector

The MCP surface of the Contract Analyzer: seven tools over the HTTP API, for a
chat client that speaks MCP. It imports nothing from `contract_analyzer` — it
is a client of that service, the same way the React app is.

**The documentation is [`docs/mcp.md`](../docs/mcp.md)**, with the rest of this
project's: the diagram, the tool table, and the reasoning behind the parts that
are decisions rather than plumbing.

```
mcp_connector/
├── server.py   the FastMCP server: connect-time instructions, seven tools
├── client.py   the one path to the API: trace ids, the error envelope
├── schemas.py  what a tool returns, and what it leaves out
└── config.py   defaults, MCP_PORT, and settings.json for tuning
tests/          offline (MockTransport), and against a real create_app()
```

```bash
./start.bash                 # the API, and this on MCP_PORT over HTTP
make mcp                     # stdio, by hand, against a running API
python -m pytest MCP-Connector/tests -q
```
