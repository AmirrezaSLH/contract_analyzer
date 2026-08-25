"""Configuration, assembled from three layers exactly once.

Everything the pipeline needs to be told rather than to infer lives here,
split by *why* a value is set rather than by what it controls:

- **`.env`** -- secrets and paths that differ between environments (a local
  checkout vs. the Docker container vs. CI): API keys, bind ports, `DB_PATH`,
  `RAW_DIR`, `ASSETS_DIR`, `LOG_FILE`. See `.env.example`.
- **`settings.json`** -- tuning parameters: the same value everywhere a given
  checkout runs, versioned with the code. Everything else lives here.
- Field defaults on `Settings` below, as the last fallback for either file.

`get_settings()` reads both files once and caches the result. The one setting
that deserves attention is `embedding_dim`: a `vec0` virtual table fixes its
vector width at creation time, so this value is baked into the schema and
cannot be changed without rebuilding the database. See `db.py`, which refuses
to open a database whose stored width disagrees with this setting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# src/contract_analyzer/config.py -> src/contract_analyzer -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

EmbeddingProvider = Literal["openai", "local", "fake"]
RetrievalMode = Literal["hybrid", "vector", "keyword"]
#: `output_config.effort` -- how hard the answer model thinks. Not `format`:
#: that one is incompatible with citations and returns a 400.
AnswerEffort = Literal["low", "medium", "high", "xhigh", "max"]

#: Model used when EMBEDDING_MODEL is left blank. Every chunk row records the
#: model that produced its vector, so this name ends up in the database.
DEFAULT_EMBEDDING_MODELS: dict[str, str] = {
    "openai": "text-embedding-3-small",
    "local": "BAAI/bge-small-en-v1.5",
    # Hashed, offline, meaningless as retrieval -- see embeddings/fake.py. The
    # name is recorded on every row it writes, so a database built with it can
    # never be mistaken for a real one.
    "fake": "fake-hash",
}

#: Widths a provider's default model can actually emit. `openai` truncates
#: (Matryoshka), the others do not -- so a mismatch here is a config error, not
#: a runtime surprise halfway through ingesting a corpus.
_FIXED_DIMS: dict[str, int] = {
    "local": 384,
}
# `openai` truncates and `fake` synthesises, so both accept any width and are
# deliberately absent above.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        json_file=PROJECT_ROOT / "settings.json",
        json_file_encoding="utf-8",
        extra="ignore",
        protected_namespaces=(),  # we have legitimate `embedding_model` fields
    )

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Env/.env (secrets, paths) outrank settings.json (tuning), which
        # outranks the field defaults below. settings.json is optional: a
        # missing file just falls through to those defaults.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # ===== .env: secrets and environment-dependent paths ===================
    # SecretStr, not str: a pydantic model prints all of its fields, so a
    # pytest failure or a logged `Settings` would otherwise put the key in the
    # terminal and in CI output. Readers call `.get_secret_value()`.
    anthropic_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    #: Relative paths anchor to the project root -- see `_anchor_to_project_root`
    #: below -- so a local checkout, the Docker container, and CI can each set
    #: these to their own mount points without the pipeline noticing.
    db_path: Path = Path("data/contracts.db")
    raw_dir: Path = Path("data/raw")
    #: Where the parser writes figure images. Chunks cite them by a path
    #: relative to the project root, so the database stays portable.
    assets_dir: Path = Path("data/assets")
    #: One JSON object per line. Blank disables the file; the console stays.
    log_file: Path | None = Path(".run/app.jsonl")
    #: `X-API-Key` the HTTP API demands. Unset means open, which is the local
    #: demo; a deployment sets it. A secret, so it lives here and not in
    #: settings.json -- see docs/api.md on what production would use instead.
    api_key: SecretStr | None = None
    #: Host ports. The API process also serves the built front end, so
    #: `BACKEND_PORT` is the only one a demo needs; `FRONTEND_PORT` is the Vite
    #: dev server (`./start.bash --dev`, `make ui-dev`). Launchers -- this
    #: package does not bind a socket itself -- read the same fields uvicorn
    #: and Vite do, so a moved port cannot drift between Python and the shell.
    backend_port: int = Field(default=8100, gt=0, lt=65536)
    frontend_port: int = Field(default=8101, gt=0, lt=65536)

    # ===== settings.json: tuning parameters =================================
    # Same value everywhere a given checkout runs, so these are versioned with
    # the code rather than read from the environment. See `settings.json`.

    # Generation
    answer_model: str = "claude-sonnet-5"
    #: The compliance analysis: five criterion runs, each with a tool loop and
    #: a structured finisher. Independent of `answer_model` so an experiment
    #: can cheapen (or spend on) analysis without moving chat, and the other
    #: way around. Not an allowlist -- nothing outside this process picks it.
    analysis_model: str = "claude-sonnet-5"
    #: Models a *client* may ask for on `POST /chat`. An allowlist, not a
    #: suggestion: chat is open when API_KEY is unset, and a free-text model id
    #: on an open endpoint is a request to spend money on an arbitrary model.
    #: Published by `GET /health` so a UI renders its picker from the server
    #: rather than from a list of its own that can drift.
    chat_models: list[str] = Field(
        default_factory=lambda: ["claude-opus-5", "claude-sonnet-5", "claude-haiku-4-5"]
    )
    #: Streaming, so this can be generous; an answer over five short passages
    #: never approaches it, and a truncated answer is worse than a slow one.
    answer_max_tokens: int = Field(default=8000, gt=0)
    #: Effort is a per-surface setting. A follow-up question answered from a
    #: few retrieved passages is not a reasoning problem, so chat runs `low`;
    #: the compliance analysis is where to spend. Thinking itself stays on --
    #: `effort` is the only lever this model accepts (see docs/generation.md).
    chat_effort: AnswerEffort = "low"
    analysis_effort: AnswerEffort = "medium"
    #: Hard caps on the agent loop. Not getting stuck is enforced by counters,
    #: not by prompting: a run that hits one is finished with what it has and
    #: marked `ended_by="cap"`.
    chat_max_tool_calls: int = Field(default=4, gt=0)
    #: Per criterion; five criteria run per contract.
    analysis_max_tool_calls: int = Field(default=8, gt=0)
    #: The evidence ledger's total, across every tool result in one run.
    max_evidence_tokens: int = Field(default=12_000, gt=0)
    #: Rounds of structural self-correction before a bad quote is dropped and
    #: the result flagged `needs_review`.
    structure_fix_rounds: int = Field(default=2, ge=0)
    #: Tool calls a `research` revision may add *on top of what the first leg
    #: already spent*. A delta, not a fresh allowance: the Router asked for a
    #: specific gap to be searched for, not for the criterion to be rerun.
    research_extra_tool_calls: int = Field(default=3, ge=0)
    #: The critic. Empty means "the same model that did the analysis", which is
    #: the honest default: the check that matters is that the critic sees only
    #: the quotes and the claims, not that it is a different vendor. Point it
    #: at another model to buy independence as well as isolation.
    evaluator_model: str = ""
    #: The critic reads a page of quotes and answers a fixed schema. That is
    #: reading, not deduction, so it does not need the analyst's effort.
    evaluator_effort: AnswerEffort = "medium"
    #: The findings are a bounded structure, but a judgement per *(quote,
    #: sub-requirement)* pair with a note each, plus thinking, outgrew 2000:
    #: a live run truncated the critic on three criteria out of five, and a
    #: truncation does not clear on retry the way load does. Half the
    #: analyst's budget keeps the critic cheap without starving it.
    evaluator_max_tokens: int = Field(default=4000, gt=0)
    #: Revision rounds the Router may spend after the first analysis. One is
    #: the honest default: it demonstrates the mechanism, and the KPI page's
    #: revise rate is what would justify raising it.
    router_max_rounds: int = Field(default=1, ge=0)
    #: Criteria analysed in parallel inside one document run. Five criteria at
    #: five workers is ~60 s wall clock instead of ~190 s sequential. It is
    #: also the outbound rate limit: with the API's `api_workers` jobs in
    #: flight, `api_workers * analysis_workers` requests can be open at once.
    #: Tests set it to 1, because the scripted transport is a FIFO.
    analysis_workers: int = Field(default=5, gt=0)
    #: The prompt library. Point it elsewhere to re-aim the assistant at
    #: another domain without touching the package -- see generation/prompts.py.
    prompts_path: Path = Path("src/contract_analyzer/generation/prompts.json")

    # HTTP API -- see api/ and docs/api.md
    #: Analysis jobs in flight. SQLite serialises writes, so two is the honest
    #: ceiling for a single-file store; a third request sees `queued`.
    #: `api_workers * analysis_workers` is the concurrent-request ceiling
    #: against the answer model, and the only rate limit a local demo has.
    api_workers: int = Field(default=2, gt=0)
    #: Rejected with 413, enforced while the body streams to disk rather than
    #: after: `await file.read()` on an endpoint that is open by default is a
    #: one-line way to run the container out of memory.
    api_max_upload_mb: float = Field(default=25.0, gt=0)
    #: The UI is a different origin only outside Docker, where compose puts
    #: both behind one network. Empty means no CORS headers at all.
    api_cors_origins: list[str] = Field(default_factory=list)
    #: An SSE comment this often, so a proxy does not close an analysis stream
    #: that is thinking rather than talking.
    api_keepalive_seconds: float = Field(default=15.0, gt=0)
    #: Per-subscriber queue depth, and how many events a client that connects
    #: late gets replayed. Oldest dropped on overflow -- a stalled reader must
    #: never block a criterion thread.
    api_event_buffer: int = Field(default=256, gt=0)
    #: How often the Monitor host sampler writes a `system_samples` row.
    #: 30 s is enough for a disk-fill slope without a row per request.
    monitor_sample_seconds: float = Field(default=30.0, gt=0)

    # HTTP -- every external call goes through http_client.py
    http_timeout_seconds: float = Field(default=60.0, gt=0)
    #: Retries after the first attempt: 3 means up to four requests.
    http_retries: int = Field(default=3, ge=0)

    # Logging
    log_level: str = "INFO"

    # Embeddings
    embedding_provider: EmbeddingProvider = "openai"
    embedding_model: str | None = None
    embedding_dim: int = Field(default=512, gt=0)

    # Chunking
    #: Clauses are short; 400 tokens keeps a chunk to one or two sub-clauses so
    #: a citation points at the obligation, not at the whole section.
    chunk_tokens: int = Field(default=400, gt=0)
    chunk_overlap_tokens: int = Field(default=80, ge=0)

    # Retrieval
    #: What `retrieve()` does when the caller does not say. Callers override it per
    #: request, so this is the default, not the policy.
    retrieval_mode: RetrievalMode = "hybrid"
    #: Per retriever, before fusion. Deeper than top_k on purpose: RRF can only
    #: rank a chunk that at least one side returned.
    retrieval_candidates: int = Field(default=20, gt=0)
    retrieval_top_k: int = Field(default=6, gt=0)
    rrf_k: int = Field(default=60, gt=0)

    @field_validator("evaluator_model")
    @classmethod
    def _evaluator_model_defaults_to_the_analyst(cls, v: str, info) -> str:
        """An unset critic model means the analyst's, resolved once here rather
        than at every call site -- `settings.evaluator_model` is always the
        model that will actually answer."""
        return v or info.data.get("analysis_model") or ""

    @field_validator("chat_models")
    @classmethod
    def _answer_model_is_offerable(cls, v: list[str], info) -> list[str]:
        # The configured default must be in its own allowlist, or the one model
        # the API answers with by default is the one model a client cannot ask
        # for -- and the UI's picker would show a value it cannot round-trip.
        answer_model = info.data.get("answer_model")
        if answer_model and answer_model not in v:
            return [answer_model, *v]
        return v

    @field_validator("api_cors_origins", mode="before")
    @classmethod
    def _split_origins(cls, v: object) -> object:
        # `API_CORS_ORIGINS=http://a,http://b` in an .env is a string.
        if isinstance(v, str):
            return [o.strip() for o in v.split(",") if o.strip()]
        return v

    @field_validator("embedding_model", "log_file", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # An empty .env line ("EMBEDDING_MODEL=") means "use the default".
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("db_path", "raw_dir", "assets_dir", "prompts_path", "log_file")
    @classmethod
    def _anchor_to_project_root(cls, v: Path | None) -> Path | None:
        # Relative paths must not depend on where the process was launched:
        # `make ingest`, pytest and the API all resolve them identically.
        if v is None:
            return None
        return v if v.is_absolute() else PROJECT_ROOT / v

    @property
    def anthropic_key(self) -> str | None:
        """The generation key in the clear, at the one point of use."""
        return self.anthropic_api_key.get_secret_value() if self.anthropic_api_key else None

    @property
    def openai_key(self) -> str | None:
        return self.openai_api_key.get_secret_value() if self.openai_api_key else None

    @property
    def api_key_value(self) -> str | None:
        """The API key in the clear, at the one point of comparison."""
        return self.api_key.get_secret_value() if self.api_key else None

    @property
    def max_upload_bytes(self) -> int:
        return int(self.api_max_upload_mb * 1024 * 1024)

    @property
    def resolved_embedding_model(self) -> str:
        return self.embedding_model or DEFAULT_EMBEDDING_MODELS[self.embedding_provider]

    def validate_embedding_dim(self) -> None:
        """Raise if the configured width is one the active provider cannot emit."""
        fixed = _FIXED_DIMS.get(self.embedding_provider)
        if fixed is not None and self.embedding_dim != fixed:
            raise ValueError(
                f"EMBEDDING_PROVIDER={self.embedding_provider} emits {fixed}-dim vectors, "
                f"but EMBEDDING_DIM={self.embedding_dim}. Fix one or the other."
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings. Cached so .env and settings.json are parsed once."""
    return Settings()
