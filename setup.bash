#!/usr/bin/env bash
# Make a checkout runnable locally. After this, `./start.bash` is enough.
#
#   ./setup.bash          venv, Python extras, Node modules, .env, UI bundle
#   ./setup.bash --clean  drop .venv and ui/node_modules first, then the same
#
# What it installs, and why that is enough:
#
#   * **Python `.venv` with `.[dev]`** — `dev` pulls in `api` (FastAPI/uvicorn)
#     and `mcp` (the connector). That is the same set `make venv` installs, so
#     `./start.bash`, `make test` and `make mcp` all work from one install.
#     The optional `[local]` extra (sentence-transformers / torch) is not
#     installed; embeddings default to OpenAI. Set keys in `.env`.
#   * **`npm ci` in `ui/`** — the lockfile, not a floating `npm install`.
#   * **`.env` from `.env.example`** if missing. Existing `.env` is left
#     alone. Keys stay blank until you fill them; the API still starts.
#   * **The production UI bundle** into `src/contract_analyzer/api/static/`,
#     so `make api` can serve `/` without a separate `make ui-build`.
#     `./start.bash` rebuilds this anyway; doing it here fails setup if the
#     front end does not compile.
#
# Not installed: Docker, system packages, Node itself. Python ≥ 3.11 and
# Node (with npm) must already be on PATH.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CLEAN=0
for arg in "$@"; do
    case "$arg" in
        --clean) CLEAN=1 ;;
        -h|--help)
            printf 'usage: %s [--clean]\n' "$0"
            exit 0
            ;;
        *) printf 'usage: %s [--clean]\n' "$0" >&2; exit 2 ;;
    esac
done

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
info() { printf '  %s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '\033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# One key at a time: `source .env` would evaluate an API key containing `#`
# or `$` as a shell expression.
dotenv() {
    local key=$1
    sed -n "s/^${key}=//p" "$ROOT/.env" 2>/dev/null | tail -1
}

# The interpreter that *creates* `.venv`. Never the venv itself: `--clean`
# deletes that path, and even without `--clean` `python3 -m venv` on an
# existing venv is how you refresh it, not how you bootstrap it.
is_project_venv() {
    local resolved
    resolved=$(readlink -f "$1" 2>/dev/null || true)
    [[ -n "$resolved" && "$resolved" == "$ROOT/.venv/"* ]]
}

find_python() {
    local bin resolved
    if [[ -n "${PYTHON:-}" ]]; then
        command -v "$PYTHON" >/dev/null 2>&1 || die "PYTHON=$PYTHON is not on PATH."
        if is_project_venv "$PYTHON"; then
            warn "PYTHON=$PYTHON is this project's venv; looking for a system 3.11+ instead."
        else
            "$PYTHON" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)' \
                || die "PYTHON=$PYTHON is older than 3.11 (required)."
            printf '%s\n' "$PYTHON"
            return 0
        fi
    fi
    for bin in python3.13 python3.12 python3.11 python3; do
        command -v "$bin" >/dev/null 2>&1 || continue
        resolved=$(command -v "$bin")
        is_project_venv "$resolved" && continue
        if "$bin" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
            printf '%s\n' "$bin"
            return 0
        fi
    done
    return 1
}

node_major() {
    node -p 'process.versions.node.split(".")[0]' 2>/dev/null
}

bold "Setting up Contract Analyzer"

# -- toolchain --------------------------------------------------------------

PY="$(find_python)" || die "Python ≥ 3.11 is required. Install it, or set PYTHON= to a 3.11+ interpreter."
py_ver="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
ok "Python   $PY ($py_ver)"

command -v npm >/dev/null 2>&1 || die "npm is not on PATH. Install Node.js 18+ (includes npm)."
command -v node >/dev/null 2>&1 || die "node is not on PATH. Install Node.js 18+."
node_maj="$(node_major || true)"
[[ -n "$node_maj" ]] || die "Could not read the Node.js version."
((node_maj >= 18)) || die "Node.js ≥ 18 is required (found $(node -v)). Vite 6 will not run on older Node."
ok "Node     $(node -v)  ·  npm $(npm -v)"

[[ -f "$ROOT/pyproject.toml" ]] || die "No pyproject.toml. Run this from the repository root."
[[ -f "$ROOT/ui/package.json" ]] || die "No ui/package.json. This checkout has no front end."
[[ -f "$ROOT/ui/package-lock.json" ]] || die "No ui/package-lock.json. Cannot npm ci."
[[ -f "$ROOT/.env.example" ]] || die "No .env.example."

if ((CLEAN)); then
    info "Cleaning  .venv and ui/node_modules"
    rm -rf "$ROOT/.venv" "$ROOT/ui/node_modules"
fi

# -- layout -----------------------------------------------------------------

mkdir -p "$ROOT/data/raw" "$ROOT/data/assets" "$ROOT/.run"
ok "Dirs     data/raw  data/assets  .run"

if [[ ! -f "$ROOT/.env" ]]; then
    cp "$ROOT/.env.example" "$ROOT/.env"
    ok "Env      wrote .env from .env.example (fill in the API keys)"
else
    ok "Env      .env already present (left unchanged)"
fi

# -- Python -----------------------------------------------------------------

VENV_PY="$ROOT/.venv/bin/python"
if [[ ! -x "$VENV_PY" ]]; then
    info "Python   creating .venv"
    "$PY" -m venv "$ROOT/.venv"
fi
ok "Python   venv at $ROOT/.venv"

info "Python   upgrading pip, then pip install -e \".[dev]\""
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install -e ".[dev]"
"$VENV_PY" -c 'import contract_analyzer, uvicorn, mcp_connector' \
    || die "The venv imported, but contract_analyzer / uvicorn / mcp_connector did not. pip install -e \".[dev]\" failed."
ok "Python   contract_analyzer, uvicorn, mcp_connector"

# -- Front end --------------------------------------------------------------

info "UI       npm ci in ui/"
npm --prefix "$ROOT/ui" ci
[[ -d "$ROOT/ui/node_modules" ]] || die "ui/node_modules is still missing after npm ci."
ok "UI       node_modules"

info "UI       building into api/static/"
npm --prefix "$ROOT/ui" run build \
    || die "The front-end build failed. Fix that, then run setup again."
ok "UI       bundle ready"

# -- keys -------------------------------------------------------------------

anthropic="$(dotenv ANTHROPIC_API_KEY)"
openai="$(dotenv OPENAI_API_KEY)"
if [[ -z "$anthropic" ]]; then
    warn "ANTHROPIC_API_KEY is blank: upload and the library work; chat and analysis do not."
fi
if [[ -z "$openai" ]]; then
    warn "OPENAI_API_KEY is blank: ingest that embeds with OpenAI will fail until it is set."
    warn "Or set embedding_provider to \"fake\" in settings.json for a keyless demo."
fi

backend_port="$(dotenv BACKEND_PORT)"
backend_port="${backend_port:-8100}"

echo
bold "Ready."
info "Start:   ./start.bash          (API + built UI on :$backend_port, MCP on :8102)"
info "         ./start.bash --dev    (Vite on :8101, proxying /api)"
info "Stop:    ./stop.bash"
info "Keys:    edit .env  (ANTHROPIC_API_KEY, OPENAI_API_KEY)"
