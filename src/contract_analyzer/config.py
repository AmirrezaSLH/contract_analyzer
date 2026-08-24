"""Configuration, read from the environment (and .env) exactly once.

Everything the pipeline needs to be told rather than to infer lives here. The
one setting that deserves attention is `embedding_dim`: a `vec0` virtual table
fixes its vector width at creation time, so this value is baked into the schema
and cannot be changed without rebuilding the database. See `db.py`, which
refuses to open a database whose stored width disagrees with this setting.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

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
        extra="ignore",
        protected_namespaces=(),  # we have legitimate `embedding_model` fields
    )

    # Generation
    # SecretStr, not str: a pydantic model prints all of its fields, so a
    # pytest failure or a logged `Settings` would otherwise put the key in the
    # terminal and in CI output. Readers call `.get_secret_value()`.
    anthropic_api_key: SecretStr | None = None
    answer_model: str = "claude-opus-5"
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
    #: The prompt library. Point it elsewhere to re-aim the assistant at
    #: another domain without touching the package -- see generation/prompts.py.
    prompts_path: Path = Path("src/contract_analyzer/generation/prompts.json")

    # HTTP -- every external call goes through http_client.py
    http_timeout_seconds: float = Field(default=60.0, gt=0)
    #: Retries after the first attempt: 3 means up to four requests.
    http_retries: int = Field(default=3, ge=0)

    # Logging
    log_level: str = "INFO"
    #: One JSON object per line. Blank disables the file; the console stays.
    log_file: Path | None = Path(".run/app.jsonl")

    # Embeddings
    embedding_provider: EmbeddingProvider = "openai"
    embedding_model: str | None = None
    embedding_dim: int = Field(default=512, gt=0)
    openai_api_key: SecretStr | None = None

    # Storage & chunking
    db_path: Path = Path("data/contracts.db")
    raw_dir: Path = Path("data/raw")
    #: Where the parser writes figure images. Chunks cite them by a path
    #: relative to the project root, so the database stays portable.
    assets_dir: Path = Path("data/assets")
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
    """Process-wide settings. Cached so .env is parsed once."""
    return Settings()
