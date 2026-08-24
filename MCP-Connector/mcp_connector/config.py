"""What the connector is told, and where each kind of value comes from.

The same three layers the analyzer itself uses, for the same reasons -- `.env`
for what differs between environments, `settings.json` for tuning that is
versioned with the code, field defaults underneath both. What is *not* shared
is the code: this module builds its own `BaseSettings` rather than importing
`contract_analyzer.config`, and that is the point of the whole package.

**The connector does not import the analyzer.** It is an HTTP client of the
API and nothing else -- no SQLite, no embedder, no model client, no
`PROJECT_ROOT` full of parsed contracts. Importing the analyzer's settings
would drag all of that into a process whose only job is to forward JSON, and
would quietly make `CA_API_URL` a lie the first time someone pointed this at a
deployment on another host and it kept answering from a local database.

The one file it does read is the repository's, when there is one. Reading
`settings.json` from two directories up is what makes `python -m mcp_connector`
work in a checkout without a wrapper script; a copy running anywhere else
simply finds no file and falls through to the defaults below, which are the
same values.

**`.env` carries the port and nothing else.** Where the API is and which
transport to serve are not facts about an environment -- they are decisions
whoever launches this has already made, and they arrive as flags from
`start.bash`, as service environment from compose, or as a desktop client's
config. Every field below is still read from the process environment, which is
how each of those overrides it; none of them needs a line in a file first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import (
    BaseSettings,
    JsonConfigSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

# MCP-Connector/mcp_connector/config.py -> MCP-Connector -> project root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: `stdio` is one client on this machine talking over pipes -- Claude Desktop,
#: `docker compose run mcp`. `http` is streamable HTTP on a port, which is what
#: a container serves and what more than one client can reach. Everything else
#: about the server is identical; see server.py.
Transport = Literal["stdio", "http"]


class ConnectorSettings(BaseSettings):
    """Where the API is, how to reach it, and how to serve MCP."""

    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        json_file=PROJECT_ROOT / "settings.json",
        json_file_encoding="utf-8",
        extra="ignore",
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
        # The analyzer's order, so a reader who knows one knows the other.
        return (
            init_settings,
            env_settings,
            dotenv_settings,
            JsonConfigSettingsSource(settings_cls),
            file_secret_settings,
        )

    # ===== where things are ================================================
    # Defaults that work, rather than fields a reader has to fill in. Of these
    # only `MCP_PORT` is in `.env.example`: a port is a fact about the machine,
    # while a transport and an API URL are decisions the thing doing the
    # launching has already made -- `start.bash` passes them as flags, compose
    # sets them on the service, and a desktop client's config carries its own.
    # Each is still read from the process environment, which is how those two
    # override it.

    #: The API's base URL, *without* the `/api` prefix -- `http://api:8100`,
    #: not `http://api:8100/api`. Unset means "the API on this machine", built
    #: from `BACKEND_PORT` so a moved port does not have to be spelled twice.
    ca_api_url: str | None = None
    backend_port: int = Field(default=8100, gt=0, lt=65536)
    #: The API's `X-API-Key`, when it demands one. The same secret the browser
    #: would send: this connector is a client of that API exactly as the UI is,
    #: and `API_KEY` is the analyzer's own field. Production is OAuth 2.1 and
    #: this is the demo -- see docs/mcp.md.
    api_key: SecretStr | None = None

    #: `stdio` by default, because the default caller is a desktop client that
    #: spawned this process and is holding the other end of the pipe. Anything
    #: serving a port says so.
    mcp_transport: Transport = "stdio"
    #: Only used by `http`. `0.0.0.0` inside a container, which compose sets.
    mcp_host: str = "127.0.0.1"
    mcp_port: int = Field(default=8102, gt=0, lt=65536)
    #: When set, `upload_contract(path=...)` accepts only files under this
    #: directory. Unset means any readable path, which is defensible for a
    #: stdio server the user launched themselves and for nothing else. See
    #: `server.upload_contract`.
    mcp_upload_root: Path | None = None

    # ===== settings.json: tuning ===========================================
    #: Everything except an upload. The API answers `POST /analyses` in under a
    #: second by design, so a long timeout here would only ever be a hang.
    mcp_request_timeout_seconds: float = Field(default=30.0, gt=0)
    #: An upload parses, chunks and embeds a contract before it answers -- tens
    #: of seconds for a real one. It gets its own budget rather than forcing
    #: every other call to wait as long as the slowest.
    mcp_upload_timeout_seconds: float = Field(default=300.0, gt=0)
    #: What `get_analysis` tells the host to wait before asking again. A run is
    #: 60-180 s, so a host polling on its own instincts either hammers the API
    #: or checks twice and gives up.
    mcp_poll_seconds: float = Field(default=10.0, gt=0)
    #: Passages per `search_contract`. Capped here rather than exposed as a
    #: tool argument: a model that can ask for fifty passages will, and the
    #: cost lands in its own context window.
    mcp_search_top_k: int = Field(default=6, ge=1, le=20)
    #: Bytes accepted from `upload_contract(url=...)`, before the API's own
    #: `api_max_upload_mb` gets a chance to refuse it.
    mcp_max_download_mb: float = Field(default=25.0, gt=0)

    @field_validator("ca_api_url", "api_key", "mcp_upload_root", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # An unset variable and one set to "" mean the same thing, and both
        # happen: `CA_API_URL=` left in a copied `.env`, and compose's
        # `${CA_API_URL:-}` when the host has none. Neither should become a
        # base URL of "" that fails at the first request with nothing useful to
        # say -- the analyzer's config.py makes the same call for the same
        # reason.
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def api_url(self) -> str:
        """The API root, no trailing slash, no `/api`."""
        base = self.ca_api_url or f"http://127.0.0.1:{self.backend_port}"
        return base.rstrip("/")

    @property
    def api_key_value(self) -> str | None:
        """The key in the clear, at the one point of use."""
        return self.api_key.get_secret_value() if self.api_key else None

    @property
    def max_download_bytes(self) -> int:
        return int(self.mcp_max_download_mb * 1024 * 1024)


def get_settings(**overrides: object) -> ConnectorSettings:
    """Read the layers. Not cached: the tests build several, and one process
    only ever calls this once anyway -- at startup, in `server.main`."""
    return ConnectorSettings(**overrides)  # type: ignore[arg-type]


__all__ = ["PROJECT_ROOT", "ConnectorSettings", "Transport", "get_settings"]
