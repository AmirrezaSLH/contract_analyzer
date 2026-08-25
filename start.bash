#!/usr/bin/env bash
# Start the whole app and follow its logs.
#
#   ./start.bash          build the React bundle, then the HTTP API (which
#                         serves it) plus the MCP connector on MCP_PORT
#   ./start.bash --dev    skip the bundle; plus the Vite dev server, proxying
#                         /api at the API
#   ./start.bash --no-mcp just the API
#   ./start.bash --detach start and return, leaving them running
#
# Ports come from `.env` (`BACKEND_PORT`, `FRONTEND_PORT`, `MCP_PORT`); see
# .env.example. Everything else the connector needs is a flag on the command
# line below -- where the API is, and which transport to serve -- because it is
# this script's decision rather than the environment's.
#
# **The connector is started on HTTP, never stdio.** stdio is a client's own
# subprocess reading its stdin -- a desktop client spawns `python -m
# mcp_connector` itself, from its config -- so a background stdio server would
# be a process with nobody on the other end of the pipe. Started here it is a
# port an MCP client can be pointed at, and one more surface a demo can show
# without a second terminal.
# Process env overrides the file. One process serves both in the default
# path: the front end is a static bundle the API builds and mounts, so the
# browser only ever talks to BACKEND_PORT. FRONTEND_PORT is Vite, and only
# with `--dev`.
#
# **Ctrl-C is the way out.** The trap stops the servers before this script
# exits, so the normal path leaves nothing behind and ./stop.bash is a safety
# net rather than a step -- for the times the trap cannot run, which is a
# `kill -9`, a closed terminal, or a `--detach` from earlier.
#
# Three things this does that `make api & make ui-dev &` does not:
#
#   * **Each server gets its own process group** (`setsid`), and the group id
#     is what goes in the pidfile. uvicorn and Vite both spawn children, and
#     killing the parent alone leaves those children holding the port --
#     which is the usual way a restart fails with "address already in use".
#   * **Vite is not started until the API answers /health.** The proxy
#     forwards `/api` at it, and a UI that boots into a dead proxy is a worse
#     first impression than three seconds of waiting.
#   * **Either server exiting on its own stops the other.** Half the app
#     running is not a state worth leaving a demo in, and a log that has simply
#     stopped scrolling is not an obvious symptom.
#
# Docker is the other way to run this (`make docker-up`) and gets the same
# ordering from `condition: service_healthy`. This script is for running
# against a local checkout without building an image.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# One key at a time: `source .env` would evaluate an API key containing `#`
# or `$` as a shell expression.
dotenv() {
    local key=$1
    sed -n "s/^${key}=//p" "$ROOT/.env" 2>/dev/null | tail -1
}

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
API_HOST="${API_HOST:-127.0.0.1}"
if [[ -z "${BACKEND_PORT:-}" ]]; then
    BACKEND_PORT="$(dotenv BACKEND_PORT)"
fi
BACKEND_PORT="${BACKEND_PORT:-8100}"
if [[ -z "${FRONTEND_PORT:-}" ]]; then
    FRONTEND_PORT="$(dotenv FRONTEND_PORT)"
fi
FRONTEND_PORT="${FRONTEND_PORT:-8101}"
if [[ -z "${MCP_PORT:-}" ]]; then
    MCP_PORT="$(dotenv MCP_PORT)"
fi
MCP_PORT="${MCP_PORT:-8102}"
RUN_DIR="$ROOT/.run"
API_PID_FILE="$RUN_DIR/api.pid"
UI_PID_FILE="$RUN_DIR/ui.pid"
MCP_PID_FILE="$RUN_DIR/mcp.pid"
API_LOG="$RUN_DIR/api.log"
UI_LOG="$RUN_DIR/ui.log"
MCP_LOG="$RUN_DIR/mcp.log"
#: How long to wait for /health before giving up and showing the log. An
#: import of the app pulls in pymupdf, sqlite-vec and both model SDKs, which is
#: a few seconds on a cold filesystem.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-45}"

DETACH=0
DEV=0
MCP=1
for arg in "$@"; do
    case "$arg" in
        --detach|-d) DETACH=1 ;;
        --dev) DEV=1 ;;
        --no-mcp) MCP=0 ;;
        *) printf 'usage: %s [--detach] [--dev] [--no-mcp]\n' "$0" >&2; exit 2 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# -- checks -----------------------------------------------------------------

[[ -x "$PYTHON" ]] || die "No interpreter at $PYTHON. Run 'make venv' first, or set PYTHON=."

"$PYTHON" -c 'import uvicorn' 2>/dev/null \
    || die "uvicorn is not installed. pip install -e '.[api]', or run 'make venv'."

# Not fatal, unlike uvicorn's: the API is the demo and the connector is a
# fourth surface on top of it. A checkout whose venv predates it should still
# start, and be told what to install.
if ((MCP)) && ! "$PYTHON" -c 'import mcp_connector' 2>/dev/null; then
    MCP=0
    MCP_MISSING=1
fi

# Both paths need Node: `--dev` for Vite, the default for `npm run build`.
command -v npm >/dev/null 2>&1 || die "npm is not on PATH. Needed to build or run the front end."
[[ -f "$ROOT/ui/package.json" ]] || die "No ui/package.json. This checkout has no front end."
[[ -d "$ROOT/ui/node_modules" ]] || die "ui/node_modules is missing. Run 'make ui-install' first."

port_owner() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null | head -1
    elif command -v ss >/dev/null 2>&1; then
        ss -lptnH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1
    fi
}

# A port already in use is almost always this app still running from last time,
# so say so rather than letting uvicorn fail with a bare OSError.
ports_to_claim=("API:$BACKEND_PORT")
((DEV)) && ports_to_claim+=("UI:$FRONTEND_PORT")
((MCP)) && ports_to_claim+=("MCP:$MCP_PORT")
for spec in "${ports_to_claim[@]}"; do
    name=${spec%%:*} port=${spec##*:}
    owner=$(port_owner "$port" || true)
    if [[ -n "$owner" ]]; then
        die "Port $port ($name) is already in use by pid $owner. Run ./stop.bash first."
    fi
done

mkdir -p "$RUN_DIR" data/raw data/assets

# -- start ------------------------------------------------------------------

# `setsid` so each server leads its own process group: the pidfile then holds a
# group id that can be signalled as a whole, children included.
spawn() {
    local pid_file=$1 log=$2; shift 2
    : > "$log"
    setsid "$@" >>"$log" 2>&1 &
    local pid=$!
    ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' > "$pid_file" || echo "$pid" > "$pid_file"
    echo "$pid"
}

bold "Starting Contract Analyzer"

if ! ((DEV)); then
    # The API serves whatever is in api/static/. Building here means a restart
    # picks up ui/ edits without a separate `make ui-build`.
    info "UI       building into api/static/"
    npm --prefix "$ROOT/ui" run build \
        || die "The front-end build failed. Fix that, then start again."
    ok "UI       bundle ready"
fi

api_pid=$(spawn "$API_PID_FILE" "$API_LOG" \
    "$PYTHON" -m uvicorn contract_analyzer.api.main:app \
    --host "$API_HOST" --port "$BACKEND_PORT")

# From here on a failure must not leave a half-started app behind, so the
# cleanup path is armed before the second server exists.
FOLLOWERS=()
shutting_down=0
ui_pid=""
mcp_pid=""

stop_all() {
    (($#)) && true
    ((shutting_down)) && return 0
    shutting_down=1
    # Followers first: killing the servers with the tails still attached prints
    # their shutdown lines *after* the summary, which reads like a crash.
    for pgid in "${FOLLOWERS[@]:-}"; do
        [[ -n "$pgid" ]] && kill -TERM "-$pgid" 2>/dev/null || true
    done
    "$ROOT/stop.bash" || true
}

on_interrupt() {
    # A newline first: Ctrl-C echoes `^C` at the cursor and the summary should
    # not start on that line.
    printf '\n'
    stop_all
    exit 0
}

# EXIT covers the paths a signal handler does not: `set -e` tripping, or one of
# the guards below calling exit.
((DETACH)) || trap on_interrupt INT TERM
((DETACH)) || trap 'stop_all' EXIT

info "API      pid $api_pid  ->  $API_LOG"

# Poll /health rather than sleeping: the import cost varies enough that any
# fixed sleep is either too short on a cold cache or wasted every other time.
deadline=$((SECONDS + HEALTH_TIMEOUT))
until "$PYTHON" - "$API_HOST" "$BACKEND_PORT" <<'PY' 2>/dev/null
import sys, urllib.request
host, port = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=2) as r:
    sys.exit(0 if r.status == 200 else 1)
PY
do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The API exited during startup. Last lines of %s:\n\n' "$API_LOG" >&2
        tail -20 "$API_LOG" >&2
        rm -f "$API_PID_FILE"
        exit 1
    fi
    ((SECONDS < deadline)) || {
        warn "The API did not answer /health within ${HEALTH_TIMEOUT}s; continuing anyway."
        break
    }
    sleep 0.5
done
ok "API      http://$API_HOST:$BACKEND_PORT  (docs at /docs, UI at /)"

# The API's own report of how it is configured. Printing it here means a
# missing key is visible now rather than three clicks later.
"$PYTHON" - "$API_HOST" "$BACKEND_PORT" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
host, port = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
    h = json.load(r)
print(f"           {h['embedder']} embeddings · chat {h['answer_model']} · "
      f"analysis {h.get('analysis_model', h['answer_model'])} · "
      f"{h['documents']} document(s)"
      + ("" if h.get("key_present") else "\n           \033[33m!\033[0m ANTHROPIC_API_KEY is "
         "not set: upload and the library work, analysis and chat do not."))
PY

if ((MCP)); then
    # HTTP, and pointed at the API by flag: the connector's own defaults are
    # for a client that launched it, and this script knows better than they do
    # which API it just started.
    mcp_pid=$(spawn "$MCP_PID_FILE" "$MCP_LOG" \
        "$PYTHON" -m mcp_connector \
        --transport http --host "$API_HOST" --port "$MCP_PORT" \
        --api-url "http://$API_HOST:$BACKEND_PORT")
    info "MCP      pid $mcp_pid  ->  $MCP_LOG"

    # The port, not a request: the MCP endpoint answers 406 to a plain GET --
    # it wants an SSE Accept header -- so "is it listening" is the honest probe
    # and a status code would be a misleading one.
    deadline=$((SECONDS + 30))
    until [[ -n "$(port_owner "$MCP_PORT")" ]]; do
        if ! kill -0 "$mcp_pid" 2>/dev/null; then
            printf '\n\033[31m✗\033[0m The MCP connector exited during startup. Last lines of %s:\n\n' "$MCP_LOG" >&2
            tail -20 "$MCP_LOG" >&2
            rm -f "$MCP_PID_FILE"
            exit 1
        fi
        ((SECONDS < deadline)) || { warn "The connector has not opened port $MCP_PORT yet; check $MCP_LOG."; break; }
        sleep 0.5
    done
    ok "MCP      http://$API_HOST:$MCP_PORT/mcp  (7 tools; stdio clients spawn their own)"
elif [[ -n "${MCP_MISSING:-}" ]]; then
    warn "MCP connector not installed; skipped. pip install -e \".[mcp]\" to enable it."
fi

if ((DEV)); then
    ui_pid=$(spawn "$UI_PID_FILE" "$UI_LOG" \
        env npm --prefix "$ROOT/ui" run dev)
    info "UI       pid $ui_pid  ->  $UI_LOG"

    deadline=$((SECONDS + 30))
    until [[ -n "$(port_owner "$FRONTEND_PORT")" ]]; do
        if ! kill -0 "$ui_pid" 2>/dev/null; then
            printf '\n\033[31m✗\033[0m The UI exited during startup. Last lines of %s:\n\n' "$UI_LOG" >&2
            tail -20 "$UI_LOG" >&2
            rm -f "$UI_PID_FILE"
            exit 1
        fi
        ((SECONDS < deadline)) || { warn "The UI has not opened port $FRONTEND_PORT yet; check $UI_LOG."; break; }
        sleep 0.5
    done
    ok "UI       http://localhost:$FRONTEND_PORT  (proxies /api -> :$BACKEND_PORT)"
fi

# -- detached ---------------------------------------------------------------

if ((DETACH)); then
    echo
    bold "Running in the background."
    logs="$API_LOG"
    ((MCP)) && logs="$logs $MCP_LOG"
    ((DEV)) && logs="$UI_LOG $logs"
    info "Logs:  tail -f $logs"
    info "Stop:  ./stop.bash"
    exit 0
fi

# -- foreground: follow logs ------------------------------------------------

# One follower per file, each prefixed, so two streams interleave and stay
# attributable. `setsid` again, and for the same reason: this is a `tail`
# feeding a shell loop, and killing the loop alone leaves the tail running --
# which is how a stopped script keeps printing.
follow() {
    local label=$1 colour=$2 file=$3
    setsid bash -c '
        label=$1 colour=$2 file=$3
        while IFS= read -r line; do
            printf "\033[%sm%-3s\033[0m │ %s\n" "$colour" "$label" "$line"
        done < <(tail -n +1 -f "$file")
    ' _ "$label" "$colour" "$file" &
    local pid=$!
    FOLLOWERS+=("$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || echo "$pid")")
}

echo
bold "Logs. Ctrl-C stops the servers."
echo
follow api 36 "$API_LOG"   # cyan
((MCP)) && follow mcp 33 "$MCP_LOG"   # yellow
((DEV)) && follow ui  35 "$UI_LOG"    # magenta

# The main wait. Polling rather than `wait` on the followers: what this needs to
# notice is a *server* exiting, and the servers are not children of this shell
# -- setsid detached them. Half the app running is not a state worth leaving a
# demo in, so either one going down takes the other with it.
while :; do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The API exited.\n' >&2
        break
    fi
    if [[ -n "$ui_pid" ]] && ! kill -0 "$ui_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The UI exited. Stopping the API.\n' >&2
        break
    fi
    if [[ -n "$mcp_pid" ]] && ! kill -0 "$mcp_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The MCP connector exited. Stopping the API.\n' >&2
        break
    fi
    sleep 1
done

# The EXIT trap does the stopping; this is only the status.
exit 1
