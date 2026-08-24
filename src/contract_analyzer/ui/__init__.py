"""The Streamlit front end: four surfaces over the HTTP API, and no logic.

This package parses no PDF, opens no database and calls no model. It makes
HTTP requests to ``CA_API_URL`` and renders the responses. The rule in
``docs/api.md`` -- *the API contains no logic the CLI does not have* -- extends
one hop further out here: if a handler is tempted into the UI it belongs in the
API, and if it is not in the API it belongs in the library first.

Nothing in ``ui/`` imports from ``api/``. The response shapes it needs are
JSON, and a pydantic model copied here would be a second source of truth for a
schema the API already owns.
"""

from __future__ import annotations

__all__ = ["ApiError", "ApiClient"]


def __getattr__(name: str):
    # Lazy, so `import contract_analyzer.ui` costs nothing in a process that
    # only wants the name -- the entrypoint's `require` check, for one.
    if name in __all__:
        from .client import ApiClient, ApiError

        return {"ApiClient": ApiClient, "ApiError": ApiError}[name]
    raise AttributeError(name)
