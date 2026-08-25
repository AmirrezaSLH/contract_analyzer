#!/usr/bin/env bash
# Container entrypoint: one verb per surface, so `docker compose run api <verb>`
# reads the same whether the surface exists yet or not.
#
#   api    FastAPI over uvicorn (also serves the built UI)
#   mcp    MCP connector; MCP_TRANSPORT picks stdio or HTTP on MCP_PORT
#
#   test   the offline pytest suite                (dev image)
#   lint   ruff (src, tests, scripts, MCP-Connector)
#   shell  interactive bash
#   *      anything else is exec'd verbatim
set -euo pipefail

# Bind mounts arrive owned by the host user and may be empty; the paths in
# Settings are relative to /app and are expected to exist.
mkdir -p /app/data/raw /app/data/assets /app/.run

# A missing surface should say which phase it belongs to, not raise
# ModuleNotFoundError from inside uvicorn.
require() {
    local module=$1 what=$2
    if ! python -c "import importlib.util, sys; sys.exit(0 if importlib.util.find_spec('$module') else 1)"; then
        echo "entrypoint: $what is not implemented yet (missing module '$module')." >&2
        echo "            Try: docker compose run --rm tools shell" >&2
        exit 78  # EX_CONFIG
    fi
}

verb=${1:-api}
shift || true

case "$verb" in
    api)
        require uvicorn "the HTTP API"
        require contract_analyzer.api "the HTTP API"
        exec uvicorn contract_analyzer.api.main:app \
            --host "${API_HOST:-0.0.0.0}" \
            --port "${BACKEND_PORT:-8100}" "$@"
        ;;
    mcp)
        # Its own package, in MCP-Connector/: the connector is a client of the
        # API over CA_API_URL, not a module of the analyzer. It binds a port
        # only when MCP_TRANSPORT=http; on stdio it talks over pipes.
        require mcp_connector "the MCP connector"
        exec python -m mcp_connector "$@"
        ;;
    test)
        exec python -m pytest -q "$@"
        ;;
    lint)
        exec python -m ruff check src tests scripts MCP-Connector "$@"
        ;;
    shell)
        exec bash "$@"
        ;;
    *)
        exec "$verb" "$@"
        ;;
esac
