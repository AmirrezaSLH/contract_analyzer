#!/usr/bin/env bash
# Stop everything this app started, including what it orphaned.
#
#   ./stop.bash           stop the API and, if running, the Vite dev server
#   ./stop.bash --dry-run list what would be killed, kill nothing
#
# Three passes, because a pidfile is not the whole story:
#
#   1. **The process groups in `.run/*.pid`**, written by start.bash. Signalling
#      the group rather than the pid is what takes uvicorn's and Vite's
#      children with it.
#   2. **A scan for orphans**: anything matching this project's command lines
#      whose working directory is *this* checkout. A crashed start.bash, a
#      `make api` from another terminal, or a run whose pidfile was deleted all
#      land here. This is the pass that stops "address already in use" on the
#      next start.
#   3. **Whatever still holds the ports**, as a last resort, since a port that
#      is still listening is the only failure a user actually notices.
#
# **The cwd check in pass 2 is the safety rail.** Matching `uvicorn` or
# `vite` on a command line alone would kill a colleague's unrelated server,
# or a second checkout of this repo. A process is only a candidate if its
# command line names *this project's* modules AND its working directory is this
# directory. Anything that matches the pattern but lives elsewhere is reported
# and left alone.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

# One key at a time: `source .env` would evaluate an API key containing `#`
# or `$` as a shell expression.
dotenv() {
    local key=$1
    sed -n "s/^${key}=//p" "$ROOT/.env" 2>/dev/null | tail -1
}

RUN_DIR="$ROOT/.run"
if [[ -z "${BACKEND_PORT:-}" ]]; then
    BACKEND_PORT="$(dotenv BACKEND_PORT)"
fi
BACKEND_PORT="${BACKEND_PORT:-8100}"
if [[ -z "${FRONTEND_PORT:-}" ]]; then
    FRONTEND_PORT="$(dotenv FRONTEND_PORT)"
fi
FRONTEND_PORT="${FRONTEND_PORT:-8101}"
#: Seconds between SIGTERM and SIGKILL. uvicorn's shutdown marks jobs in
#: flight as failed and closes the database rather than leaving rows saying
#: `running`, so it is worth waiting for.
GRACE="${GRACE:-8}"

DRY=0
[[ "${1:-}" == "--dry-run" || "${1:-}" == "-n" ]] && DRY=1

#: Extended regexes matching a *server invocation*, not merely a mention of one.
#: `pgrep -f` matches the whole command line, so a bare 'uvicorn' would also
#: match an editor with a log open, a `tail -f`, or the grep looking for it --
#: all of which run in this directory, so the cwd check below cannot tell them
#: apart. Requiring the interpreter and the `-m` invocation is what makes the
#: match mean "this is a running server". The UI in production is a static
#: bundle the API serves; the only separate UI process is Vite under `--dev`.
PATTERNS=(
    'python[0-9.]* +-m +uvicorn +contract_analyzer\.api\.main:app'
    # Vite via `npm run dev` in ui/: the bin path contains this checkout.
    "node .+${ROOT//./\\.}/ui/.+vite"
)

bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
info()  { printf '  %s\n' "$*"; }
ok()    { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn()  { printf '  \033[33m!\033[0m %s\n' "$*"; }

killed_any=0

# This script, the shell that launched it, and that shell's ancestors. A
# process cannot be an orphaned server if it is the thing doing the stopping,
# and killing the terminal that ran `./stop.bash` is a memorable way to learn
# that pgrep matches your own command line too.
SELF=()
_walk=$$
while [[ -n "$_walk" && "$_walk" != "0" && "$_walk" != "1" ]]; do
    SELF+=("$_walk")
    _walk=$(ps -o ppid= -p "$_walk" 2>/dev/null | tr -d ' ' || true)
done

is_self() {
    local pid=$1 mine
    for mine in "${SELF[@]}"; do
        [[ "$pid" == "$mine" ]] && return 0
    done
    return 1
}

# Vite's cwd is ui/, uvicorn's is the project root. Both are this checkout.
ours() {
    local dir=$1
    [[ "$dir" == "$ROOT" || "$dir" == "$ROOT/ui" ]]
}

# The working directory of a pid, or empty when it cannot be read (another
# user's process, or a platform without /proc).
cwd_of() {
    local pid=$1
    if [[ -r "/proc/$pid/cwd" ]]; then
        readlink -f "/proc/$pid/cwd" 2>/dev/null || true
    elif command -v lsof >/dev/null 2>&1; then
        lsof -a -p "$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1
    fi
}

cmdline_of() {
    local pid=$1
    if [[ -r "/proc/$pid/cmdline" ]]; then
        tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null
    else
        ps -o args= -p "$pid" 2>/dev/null
    fi
}

# TERM, wait up to $GRACE, then KILL. `target` is a pid or a negated pgid.
declare -A handled=()

signal_and_wait() {
    local target=$1 label=$2 probe=${3:-$1}
    probe=${probe#-}

    # Each pid is acted on once. The three passes deliberately overlap -- a
    # server found by its pidfile is found again by the scan and again by the
    # port check -- and without this a dry run would list one server three
    # times and read like three processes.
    [[ -n "${handled[$probe]:-}" ]] && return 0
    handled[$probe]=1

    if ((DRY)); then
        info "would stop $label"
        return 0
    fi
    kill -TERM "$target" 2>/dev/null || true
    local deadline=$((SECONDS + GRACE))
    while kill -0 "$probe" 2>/dev/null; do
        ((SECONDS < deadline)) || {
            warn "$label did not stop on SIGTERM; sending SIGKILL"
            kill -KILL "$target" 2>/dev/null || true
            sleep 0.5
            break
        }
        sleep 0.3
    done
    ok "stopped $label"
    killed_any=1
}

bold "Stopping Contract Analyzer"
((DRY)) && warn "dry run — nothing will be killed"

# -- pass 1: the pidfiles ---------------------------------------------------

for name in api ui; do
    pid_file="$RUN_DIR/$name.pid"
    [[ -f "$pid_file" ]] || continue
    pgid=$(tr -d '[:space:]' < "$pid_file")
    if [[ -z "$pgid" || ! "$pgid" =~ ^[0-9]+$ ]]; then
        ((DRY)) || rm -f "$pid_file"
        continue
    fi
    if is_self "$pgid"; then
        info "$name: pidfile names this shell's own group; ignoring it"
    elif kill -0 "-$pgid" 2>/dev/null || kill -0 "$pgid" 2>/dev/null; then
        # The negated pgid signals every process in the group, which is how
        # uvicorn's reloader children and Vite's children go too.
        signal_and_wait "-$pgid" "$name (process group $pgid)" "$pgid"
    else
        info "$name: pidfile named $pgid, which is not running (stale)"
    fi
    ((DRY)) || rm -f "$pid_file"
done

# -- pass 2: orphans belonging to this checkout -----------------------------

declare -A seen=()
strays=()
foreign=()

for pattern in "${PATTERNS[@]}"; do
    # -f matches the full command line. Errors are swallowed: pgrep exits 1
    # when nothing matches, which is the normal case.
    while read -r pid; do
        [[ -n "$pid" ]] || continue
        is_self "$pid" && continue
        [[ -n "${seen[$pid]:-}" ]] && continue
        seen[$pid]=1
        pid_cwd=$(cwd_of "$pid")
        if ours "$pid_cwd"; then
            strays+=("$pid")
        elif [[ -z "$pid_cwd" ]]; then
            # Cannot prove it is ours, so it is not touched. Reported, because
            # a process we refuse to kill is the likely reason a port stays
            # busy and the user deserves to know which one.
            foreign+=("$pid (working directory unreadable)")
        else
            foreign+=("$pid (in $pid_cwd)")
        fi
    done < <(pgrep -f "$pattern" 2>/dev/null || true)
done

for pid in "${strays[@]:-}"; do
    [[ -n "$pid" ]] || continue
    kill -0 "$pid" 2>/dev/null || continue   # already gone with its group
    label="orphan $pid — $(cmdline_of "$pid" | cut -c1-70)"
    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ' || true)
    if [[ -n "$pgid" && "$pgid" != "$$" ]]; then
        signal_and_wait "-$pgid" "$label" "$pid"
    else
        signal_and_wait "$pid" "$label"
    fi
done

for entry in "${foreign[@]:-}"; do
    [[ -n "$entry" ]] || continue
    warn "left alone, not this checkout: $entry"
done

# -- pass 3: anything still holding the ports -------------------------------

port_owner() {
    local port=$1
    if command -v lsof >/dev/null 2>&1; then
        lsof -ti "tcp:$port" -sTCP:LISTEN 2>/dev/null | head -1
    elif command -v ss >/dev/null 2>&1; then
        ss -lptnH "sport = :$port" 2>/dev/null | grep -oP 'pid=\K[0-9]+' | head -1
    fi
}

for spec in "API:$BACKEND_PORT" "UI:$FRONTEND_PORT"; do
    name=${spec%%:*} port=${spec##*:}
    owner=$(port_owner "$port" || true)
    [[ -n "$owner" ]] || continue
    is_self "$owner" && continue
    pid_cwd=$(cwd_of "$owner")
    if ours "$pid_cwd"; then
        signal_and_wait "$owner" "$name on port $port (pid $owner, still listening)"
    else
        warn "port $port is held by pid $owner, which is not this app — left alone"
    fi
done

echo
if ((DRY)); then
    bold "Dry run complete."
elif ((killed_any)); then
    bold "Stopped."
else
    bold "Nothing was running."
fi

# The honest final check: a port that is still listening is the one failure a
# user actually notices, so it is verified rather than assumed. A port that is
# still held is a non-zero exit, because a script that reports failure and
# returns success is worse than one that does neither.
status=0
if ((!DRY)); then
    for spec in "API:$BACKEND_PORT" "UI:$FRONTEND_PORT"; do
        name=${spec%%:*} port=${spec##*:}
        owner=$(port_owner "$port" || true)
        if [[ -n "$owner" ]]; then
            warn "port $port ($name) is still held by pid $owner"
            status=1
        fi
    done
fi
exit "$status"
