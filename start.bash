#!/usr/bin/env bash
# Start the whole app: the HTTP API on 8000, the Streamlit UI on 8501.
#
#   ./start.bash              both, in the background, and return
#   ./start.bash --foreground both, and follow the logs until Ctrl-C
#   API_PORT=9000 ./start.bash
#
# Stop it with ./stop.bash, which also finds anything this script orphaned.
#
# Two things this does that `make api & make ui &` does not:
#
#   * **Each server gets its own process group** (`setsid`), and the group id
#     is what goes in the pidfile. uvicorn and streamlit both spawn children,
#     and killing the parent alone leaves those children holding the port --
#     which is the usual way a restart fails with "address already in use".
#   * **The UI is not started until the API answers /health.** The UI calls it
#     on its first render, and a UI that boots into "the API is not reachable"
#     is a worse first impression than three seconds of waiting.
#
# Docker is the other way to run this (`make docker-up`) and does the same two
# things with `restart:` and `condition: service_healthy`. This script is for
# running against a local checkout without building an image.

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

FOREGROUND=0
[[ "${1:-}" == "--foreground" || "${1:-}" == "-f" ]] && FOREGROUND=1

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

# A port already in use is almost always this app still running from last time,
# so say so rather than letting uvicorn fail with a bare OSError.
port_owner() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null | head -1
    elif command -v ss >/dev/null 2>&1; then
        ss -lptnH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1
    fi
}

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
# group id that stop.bash can signal as a whole, children included.
spawn() {
    local pid_file=$1 log=$2; shift 2
    : > "$log"
    setsid "$@" >>"$log" 2>&1 &
    local pid=$!
    # The group id, not the pid: with setsid they are the same number, but
    # asking for it is what makes the intent explicit and survives a shell
    # without setsid, where the two differ.
    ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' > "$pid_file" || echo "$pid" > "$pid_file"
    echo "$pid"
}

bold "Starting Contract Analyzer"

api_pid=$(spawn "$API_PID_FILE" "$API_LOG" \
    "$PYTHON" -m uvicorn contract_analyzer.api.main:app \
    --host "$API_HOST" --port "$API_PORT")
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

echo
bold "Running."
info "Logs:  tail -f $UI_LOG $API_LOG"
info "       tail -f $RUN_DIR/app.jsonl    # the structured application log"
info "Stop:  ./stop.bash"

if ((FOREGROUND)); then
    echo
    bold "Following the logs. Ctrl-C stops both servers."
    # The trap is what makes --foreground honest: Ctrl-C here must not leave
    # two detached process groups behind, which is exactly the orphan case
    # stop.bash exists to clean up.
    trap 'echo; "$ROOT/stop.bash"; exit 0' INT TERM
    tail -f "$API_LOG" "$UI_LOG" &
    wait $!
fi
