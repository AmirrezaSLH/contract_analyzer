#!/usr/bin/env bash
# Start the whole app and follow its logs: the HTTP API on 8000, the Streamlit
# UI on 8501.
#
#   ./start.bash          both, logs streaming, Ctrl-C stops them
#   ./start.bash --detach start and return, leaving them running
#   API_PORT=9000 ./start.bash
#
# **Ctrl-C is the way out.** The trap stops both servers before this script
# exits, so the normal path leaves nothing behind and ./stop.bash is a safety
# net rather than a step -- for the times the trap cannot run, which is a
# `kill -9`, a closed terminal, or a `--detach` from earlier.
#
# Three things this does that `make api & make ui &` does not:
#
#   * **Each server gets its own process group** (`setsid`), and the group id
#     is what goes in the pidfile. uvicorn and streamlit both spawn children,
#     and killing the parent alone leaves those children holding the port --
#     which is the usual way a restart fails with "address already in use".
#   * **The UI is not started until the API answers /health.** The UI calls it
#     on its first render, and a UI that boots into "the API is not reachable"
#     is a worse first impression than three seconds of waiting.
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

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
API_HOST="${API_HOST:-127.0.0.1}"
API_PORT="${API_PORT:-8000}"
UI_PORT="${UI_PORT:-8501}"
RUN_DIR="$ROOT/.run"
API_PID_FILE="$RUN_DIR/api.pid"
UI_PID_FILE="$RUN_DIR/ui.pid"
API_LOG="$RUN_DIR/api.log"
UI_LOG="$RUN_DIR/ui.log"
#: How long to wait for /health before giving up and showing the log. An
#: import of the app pulls in pymupdf, sqlite-vec and both model SDKs, which is
#: a few seconds on a cold filesystem.
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-45}"

DETACH=0
case "${1:-}" in
    --detach|-d) DETACH=1 ;;
    "") ;;
    *) printf 'usage: %s [--detach]\n' "$0" >&2; exit 2 ;;
esac

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# -- checks -----------------------------------------------------------------

[[ -x "$PYTHON" ]] || die "No interpreter at $PYTHON. Run 'make venv' first, or set PYTHON=."

missing=()
"$PYTHON" -c 'import uvicorn' 2>/dev/null || missing+=("uvicorn — pip install -e '.[api]'")
"$PYTHON" -c 'import streamlit' 2>/dev/null || missing+=("streamlit — pip install -e '.[ui]'")
if ((${#missing[@]})); then
    printf '\033[31m✗\033[0m Missing dependencies:\n' >&2
    printf '    %s\n' "${missing[@]}" >&2
    die "Install them, or run 'make venv' for everything."
fi

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
for spec in "API:$API_PORT" "UI:$UI_PORT"; do
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

api_pid=$(spawn "$API_PID_FILE" "$API_LOG" \
    "$PYTHON" -m uvicorn contract_analyzer.api.main:app \
    --host "$API_HOST" --port "$API_PORT")

# From here on a failure must not leave a half-started app behind, so the
# cleanup path is armed before the second server exists.
FOLLOWERS=()
shutting_down=0

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
until "$PYTHON" - "$API_HOST" "$API_PORT" <<'PY' 2>/dev/null
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
        warn "The API did not answer /health within ${HEALTH_TIMEOUT}s; starting the UI anyway."
        break
    }
    sleep 0.5
done
ok "API      http://$API_HOST:$API_PORT  (docs at /docs)"

# The API's own report of how it is configured, which is also what the UI reads
# on its first render. Printing it here means a missing key is visible now
# rather than three clicks later.
"$PYTHON" - "$API_HOST" "$API_PORT" <<'PY' 2>/dev/null || true
import json, sys, urllib.request
host, port = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(f"http://{host}:{port}/health", timeout=3) as r:
    h = json.load(r)
print(f"           {h['embedder']} embeddings · {h['answer_model']} · "
      f"{h['documents']} document(s)"
      + ("" if h.get("key_present") else "\n           \033[33m!\033[0m ANTHROPIC_API_KEY is "
         "not set: upload and the library work, analysis and chat do not."))
PY

ui_pid=$(spawn "$UI_PID_FILE" "$UI_LOG" \
    env CA_API_URL="http://$API_HOST:$API_PORT" \
    "$PYTHON" -m streamlit run src/contract_analyzer/ui/app.py \
    --server.port "$UI_PORT" --server.headless true)
info "UI       pid $ui_pid  ->  $UI_LOG"

deadline=$((SECONDS + 30))
until [[ -n "$(port_owner "$UI_PORT")" ]]; do
    if ! kill -0 "$ui_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The UI exited during startup. Last lines of %s:\n\n' "$UI_LOG" >&2
        tail -20 "$UI_LOG" >&2
        rm -f "$UI_PID_FILE"
        exit 1
    fi
    ((SECONDS < deadline)) || { warn "The UI has not opened port $UI_PORT yet; check $UI_LOG."; break; }
    sleep 0.5
done
ok "UI       http://localhost:$UI_PORT"

# -- detached ---------------------------------------------------------------

if ((DETACH)); then
    echo
    bold "Running in the background."
    info "Logs:  tail -f $UI_LOG $API_LOG"
    info "Stop:  ./stop.bash"
    exit 0
fi

# -- foreground: follow both logs ------------------------------------------

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
bold "Logs. Ctrl-C stops both servers."
echo
follow api 36 "$API_LOG"   # cyan
follow ui  35 "$UI_LOG"    # magenta

# The main wait. Polling rather than `wait` on the followers: what this needs to
# notice is a *server* exiting, and the servers are not children of this shell
# -- setsid detached them. Half the app running is not a state worth leaving a
# demo in, so either one going down takes the other with it.
while :; do
    if ! kill -0 "$api_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The API exited. Stopping the UI.\n' >&2
        break
    fi
    if ! kill -0 "$ui_pid" 2>/dev/null; then
        printf '\n\033[31m✗\033[0m The UI exited. Stopping the API.\n' >&2
        break
    fi
    sleep 1
done

# The EXIT trap does the stopping; this is only the status.
exit 1
