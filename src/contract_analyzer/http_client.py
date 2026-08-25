"""One HTTP client, one retry policy, for every external call.

The Anthropic and OpenAI SDKs both run on :mod:`httpx`, so a single
``httpx.Client`` can be handed to each of them. That is how this module gets
its guarantee: **every request that leaves the process goes through
:class:`RetryingTransport`**, and the SDKs are built with ``max_retries=0`` so
there is exactly one retry loop, in one place, tested and logged.

The policy:

* Retry on connection errors, timeouts, and on HTTP 408, 409, 429, 500, 502,
  503, 504. Do not retry on any other 4xx -- a 400 or 401 will not get better
  by asking again.
* ``retries`` attempts after the first (default 3), waiting ``base * 2**n``
  seconds with full jitter: about 1s, 2s, 4s. A ``Retry-After`` header wins
  over the computed delay.
* Every retry is logged as ``http.retry`` with the attempt number, the wait,
  and the reason, under whatever trace id the caller set.
* When the policy is exhausted the caller gets :class:`HttpFailure` -- one
  line naming the method, URL, last status/error, attempts and elapsed time --
  rather than a stack of SDK internals. Callers that want to distinguish a
  network failure from an API error can inspect ``.status`` and ``.cause``.

Deliberately not here: circuit breaking, per-host budgets, response caching.
A local demo with two upstreams does not need them, and each is an easy layer
on top of a transport that already centralises the traffic.
"""

from __future__ import annotations

import random
import time
from collections.abc import Iterable

import httpx2 as httpx  # the SDKs run on httpx2; same API as httpx

from .logger import get_logger, span

log = get_logger(__name__)

RETRY_STATUSES: frozenset[int] = frozenset({408, 409, 429, 500, 502, 503, 504})
RETRY_EXCEPTIONS: tuple[type[Exception], ...] = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)

DEFAULT_TIMEOUT = 60.0
DEFAULT_RETRIES = 3
DEFAULT_BACKOFF_BASE = 1.0
MAX_BACKOFF = 30.0


class HttpFailure(RuntimeError):
    """A request that did not succeed after the retry policy was exhausted."""

    def __init__(
        self,
        method: str,
        url: str,
        *,
        attempts: int,
        elapsed: float,
        status: int | None = None,
        cause: Exception | None = None,
    ) -> None:
        self.method = method
        self.url = url
        self.attempts = attempts
        self.elapsed = elapsed
        self.status = status
        self.cause = cause
        reason = f"HTTP {status}" if status is not None else f"{type(cause).__name__}: {cause}"
        super().__init__(
            f"{method} {url} failed after {attempts} attempt(s) in {elapsed:.1f}s: {reason}"
        )


def unwrap_http_failure(exc: BaseException) -> HttpFailure | None:
    """The :class:`HttpFailure` an SDK wrapped, if that is what ``exc`` hides.

    Both SDKs catch whatever the transport raised and re-raise it as their own
    ``APIConnectionError("Connection error.")`` with the original as
    ``__cause__``. The one-line diagnostic this module promises -- method,
    URL, attempts, elapsed -- is therefore one level down. A caller does::

        except sdk.APIConnectionError as exc:
            failure = unwrap_http_failure(exc)
            if failure is not None:
                raise failure from None
            raise

    Returns ``None`` when the cause is something else (a DNS error the policy
    does not cover, say), so the caller re-raises the SDK's own error unchanged.
    """
    cause = exc.__cause__
    return cause if isinstance(cause, HttpFailure) else None


def retry_after_seconds(response: httpx.Response) -> float | None:
    """The server's ``Retry-After`` in seconds, if it sent one we can read."""
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        # An HTTP-date. httpx does not parse it for us; treat as unknown.
        return None


def backoff_delay(attempt: int, *, base: float = DEFAULT_BACKOFF_BASE, rng=random) -> float:
    """Full-jitter exponential backoff: uniform(0, base * 2**attempt), capped.

    Jitter matters when many workers retry at once -- the whole point of a 429
    is that the server wants the load spread out, not synchronised.
    """
    ceiling = min(MAX_BACKOFF, base * (2**attempt))
    return rng.uniform(ceiling / 2, ceiling)


class RetryingTransport(httpx.BaseTransport):
    """Wrap a transport with the retry policy. Sync only; nothing here is async."""

    def __init__(
        self,
        inner: httpx.BaseTransport | None = None,
        *,
        retries: int = DEFAULT_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        retry_statuses: Iterable[int] = RETRY_STATUSES,
        sleep=time.sleep,
    ) -> None:
        self._inner = inner or httpx.HTTPTransport()
        self._retries = max(0, retries)
        self._base = backoff_base
        self._statuses = frozenset(retry_statuses)
        self._sleep = sleep

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        method, url = request.method, str(request.url)
        with span("upstream.call", log, method=method, url=url) as bag:
            return self._exchange(request, bag)

    def _exchange(self, request: httpx.Request, bag: dict) -> httpx.Response:
        method, url = request.method, str(request.url)
        started = time.perf_counter()
        last_status: int | None = None
        last_exc: Exception | None = None
        reason = ""

        for attempt in range(self._retries + 1):
            bag["attempts"] = attempt + 1
            try:
                response = self._inner.handle_request(request)
            except RETRY_EXCEPTIONS as exc:
                last_exc, last_status = exc, None
                reason = type(exc).__name__
                delay = None
            else:
                if response.status_code not in self._statuses:
                    return response
                last_status, last_exc = response.status_code, None
                reason = f"HTTP {response.status_code}"
                delay = retry_after_seconds(response)
                response.close()

            if attempt == self._retries:
                break
            wait = delay if delay is not None else backoff_delay(attempt, base=self._base)
            log.warning(
                "http.retry",
                extra={
                    "method": method,
                    "url": url,
                    "attempt": attempt + 1,
                    "max_attempts": self._retries + 1,
                    "wait_s": round(wait, 2),
                    "reason": reason,
                },
            )
            with span("upstream.retry", log, reason=reason, attempt=attempt + 1):
                pass
            self._sleep(wait)

        elapsed = time.perf_counter() - started
        failure = HttpFailure(
            method,
            url,
            attempts=self._retries + 1,
            elapsed=elapsed,
            status=last_status,
            cause=last_exc,
        )
        log.error("http.failed", extra={"method": method, "url": url, "error": str(failure)})
        with span("upstream.failed", log, reason=reason):
            pass
        raise failure

    def close(self) -> None:
        self._inner.close()


def build_http_client(
    *,
    timeout: float = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
    backoff_base: float = DEFAULT_BACKOFF_BASE,
    transport: httpx.BaseTransport | None = None,
    **kwargs,
) -> httpx.Client:
    """A client whose every request goes through :class:`RetryingTransport`.

    ``transport`` is the *inner* transport -- tests pass ``httpx.MockTransport``
    and still exercise the retry loop around it.
    """
    return httpx.Client(
        timeout=httpx.Timeout(timeout),
        transport=RetryingTransport(transport, retries=retries, backoff_base=backoff_base),
        **kwargs,
    )


_shared: httpx.Client | None = None


def get_http_client(settings=None) -> httpx.Client:
    """The process-wide client, configured from settings on first use.

    Shared so the connection pool is shared: the embedder and the answer
    model should not each open their own sockets.
    """
    global _shared
    if _shared is None:
        if settings is None:
            from .config import get_settings

            settings = get_settings()
        _shared = build_http_client(
            timeout=settings.http_timeout_seconds, retries=settings.http_retries
        )
    return _shared


def request(method: str, url: str, **kwargs) -> httpx.Response:
    """A one-off request through the shared client, for anything outside the SDKs.

    Raises :class:`HttpFailure` when the retry policy is exhausted, and
    :class:`httpx.HTTPStatusError` for a non-retryable error status, so a
    caller never has to inspect ``status_code`` to know whether it succeeded.
    """
    response = get_http_client().request(method, url, **kwargs)
    response.raise_for_status()
    return response
