"""The one way this process reaches the API, and the one way failures come back.

Every tool in `server.py` is `parse arguments -> call one HTTP operation ->
shape the result`. This module is the middle third, and it exists so three
decisions are made once rather than seven times.

**Every call carries a trace id, and the API returns it.** The middleware in
`api/main.py` honours an incoming `X-Trace-Id`, so one tool call, the HTTP
request it makes, the five criterion runs that request starts and every search
those agents make all share one id in `.run/app.jsonl`. Minting it here rather
than letting the API mint it is what lets a failed tool result *name* the id
the host can quote back.

**Every failure is the API's envelope.** `{"error": {"code", "message",
"hint"}}` is the shape `api/errors.py` guarantees, `code` is the stable thing
to branch on, and `hint` is a sentence written to be read by exactly the two
readers this connector has: a model deciding what to do next, and the person
watching it. `ApiFailure` carries all three to `server.py`, which turns it into
an MCP error. A traceback never reaches the host.

**Nothing is ever written to stdout.** On the stdio transport, stdout *is* the
JSON-RPC stream: one stray `print` and the client sees a protocol error rather
than a tool result. The logger configured in `server.py` writes to stderr, and
this module uses it.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import unquote, urlparse

import httpx

from .config import ConnectorSettings

log = logging.getLogger(__name__)

#: The API's prefix. Every route is behind it; `/health` also answers at the
#: root, but there is no reason for this client to know two spellings.
API_PREFIX = "/api"

#: What a failure that never reached the API looks like to a host. Not a
#: transport detail: "the analyzer is not running" is a thing the person
#: driving the host can fix, and the hint has to say so.
UNREACHABLE = "api_unreachable"

#: A URL upload that is not a PDF is refused here rather than after 25 MB have
#: been pulled through this process and posted to the API.
PDF_MAGIC = b"%PDF"


@dataclass
class ApiFailure(Exception):
    """A failed call, in the API's own vocabulary.

    `status` is carried but is not what a caller should branch on: `code` is
    stable across versions and says what happened, where 404 says only that
    something was missing.
    """

    code: str
    message: str
    hint: str | None = None
    status: int | None = None
    trace_id: str | None = None

    def __str__(self) -> str:
        parts = [f"{self.code}: {self.message}"]
        if self.hint:
            parts.append(self.hint)
        if self.trace_id:
            parts.append(f"(trace {self.trace_id})")
        return " ".join(parts)


@dataclass
class ApiClient:
    """A thin, retrying-nothing HTTP client for one Contract Analyzer.

    **No retries.** `http_client.py` in the analyzer retries because it calls
    model providers over the internet; this calls a service that is usually on
    the same host or the same compose network, and the two operations worth
    retrying -- an upload and an analysis -- are the two where a blind retry
    means paying twice. A host that gets `api_unreachable` can call the tool
    again itself, having been told what is wrong.
    """

    settings: ConnectorSettings
    #: A test seam, not configuration -- the same one `build_http_client` and
    #: `create_app` give the analyzer's tests. Injected, every request in this
    #: process is answered by the handler instead of a socket, so the whole
    #: tool surface can be driven with no network and no keys.
    transport: httpx.BaseTransport | None = None
    _client: httpx.Client | None = field(default=None, init=False, repr=False)

    # -- lifecycle ---------------------------------------------------------

    @property
    def client(self) -> httpx.Client:
        """The connection pool, built once and kept.

        Lazily, like `get_http_client` in the analyzer: the server builds this
        before it serves anything, but a test that never makes a call should
        not open a socket to find that out.
        """
        if self._client is None:
            headers = {"Accept": "application/json"}
            key = self.settings.api_key_value
            if key:
                headers["X-API-Key"] = key
            self._client = httpx.Client(
                base_url=f"{self.settings.api_url}{API_PREFIX}",
                headers=headers,
                timeout=self.settings.mcp_request_timeout_seconds,
                follow_redirects=True,
                transport=self.transport,
            )
        return self._client

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def __enter__(self) -> ApiClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- the one call ------------------------------------------------------

    def call(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> Any:
        """One request. Returns parsed JSON, or raises `ApiFailure`.

        The trace id is minted per call rather than per session: a session is
        the host's conversation and can run for an hour, and a trace that
        covers an hour is not a trace.
        """
        trace_id = uuid.uuid4().hex
        sent = {"X-Trace-Id": trace_id, **(headers or {})}
        try:
            response = self.client.request(
                method,
                path,
                json=json,
                params=params,
                files=files,
                headers=sent,
                timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
            )
        except httpx.RequestError as exc:
            log.warning("mcp.api_unreachable", extra={"path": path, "error": str(exc)})
            raise ApiFailure(
                UNREACHABLE,
                f"Could not reach the Contract Analyzer API at {self.settings.api_url}: {exc}.",
                "Start the API (`./start.bash`, or `make docker-up`) and try again, or "
                "point CA_API_URL at the deployment that is running.",
                trace_id=trace_id,
            ) from None

        # The API echoes the id it was given; a proxy that dropped the header
        # means the log lines are under the id the API minted instead.
        trace_id = response.headers.get("X-Trace-Id", trace_id)
        if response.is_success:
            return _body(response)
        raise _failure(response, trace_id)

    # -- upload by url -----------------------------------------------------

    def fetch_pdf(self, url: str) -> tuple[str, bytes]:
        """Download a contract the host named by URL. Refuses anything else.

        The size is checked *while* the body streams rather than after, for the
        same reason `api/uploads.py` does it: a cap enforced after the bytes
        have arrived is not a cap. What survives the checks is handed to the
        API as a normal multipart upload, so a URL upload and a file upload are
        one code path from there on.
        """
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ApiFailure(
                "validation",
                f"{url!r} is not an http(s) URL.",
                "Give a link the server can fetch, or use `path` on a stdio server.",
            )
        limit = self.settings.max_download_bytes
        chunks: list[bytes] = []
        size = 0
        try:
            with httpx.Client(
                follow_redirects=True,
                timeout=self.settings.mcp_upload_timeout_seconds,
                # Its own client: this one call leaves for an address a model
                # named, and it has no business inheriting the API's base URL
                # or its key. The transport is shared only when a test injects
                # one, which is what lets a download be scripted too.
                transport=self.transport,
            ) as fetcher, fetcher.stream("GET", url) as response:
                if not response.is_success:
                    raise ApiFailure(
                        "download_failed",
                        f"{url} answered {response.status_code}.",
                        "Check the link, or download the file and pass `path` instead.",
                    )
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > limit:
                        raise ApiFailure(
                            "too_large",
                            f"{url} is larger than the {self.settings.mcp_max_download_mb} MB "
                            "this connector will fetch.",
                            "Upload it through the web UI instead.",
                        )
                    chunks.append(chunk)
        except httpx.RequestError as exc:
            raise ApiFailure(
                "download_failed",
                f"Could not download {url}: {exc}.",
                "Check the link is reachable from the machine this connector runs on.",
            ) from None

        body = b"".join(chunks)
        if not body.startswith(PDF_MAGIC):
            raise ApiFailure(
                "not_a_pdf",
                f"{url} did not return a PDF.",
                "This service analyses PDF contracts. Give a link to the PDF itself, "
                "not to a page about it.",
            )
        return _filename_from(url), body


def _body(response: httpx.Response) -> Any:
    """The JSON body, or `None` for the one route that has none (`DELETE`)."""
    if response.status_code == 204 or not response.content:
        return None
    try:
        return response.json()
    except ValueError:
        # An HTML error page from a proxy in front of the API is the usual way
        # here, and "Expecting value: line 1 column 1" helps nobody.
        raise ApiFailure(
            "bad_response",
            f"The API answered {response.status_code} with a body that is not JSON.",
            "Check that CA_API_URL points at the Contract Analyzer API itself and "
            "not at something in front of it.",
            status=response.status_code,
        ) from None


def _failure(response: httpx.Response, trace_id: str) -> ApiFailure:
    """A non-2xx as `ApiFailure`, preferring the API's own envelope."""
    payload: Any = None
    try:
        payload = response.json()
    except ValueError:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and error.get("code"):
        return ApiFailure(
            code=str(error["code"]),
            message=str(error.get("message") or "The request failed."),
            hint=error.get("hint"),
            status=response.status_code,
            trace_id=trace_id,
        )
    # Not this API's envelope: something in front of it answered.
    return ApiFailure(
        code=f"http_{response.status_code}",
        message=f"The API answered {response.status_code}.",
        hint="Check CA_API_URL, and whether anything is proxying it.",
        status=response.status_code,
        trace_id=trace_id,
    )


def _filename_from(url: str) -> str:
    """A filename for a downloaded contract, from the URL's last path segment.

    Sanitised here as well as in the API. `api/uploads.py` is the boundary that
    actually matters and it is not being trusted to be the only one: the name
    travels through this process, and a segment like `../../etc/passwd.pdf` has
    no business being carried anywhere by a service that could have dropped it.
    """
    name = unquote(urlparse(url).path.rsplit("/", 1)[-1]) or "contract.pdf"
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).lstrip(".") or "contract.pdf"
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


__all__ = ["API_PREFIX", "ApiClient", "ApiFailure", "UNREACHABLE"]
