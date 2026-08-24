# HTTP client

`src/contract_analyzer/http_client.py` is the single path for every request
that leaves the process. Both upstreams -- Anthropic for generation, OpenAI for
embeddings -- are reached through it.

## The mechanism

The Anthropic (≥1.0) and OpenAI (≥3.0) Python SDKs are built on **`httpx2`**
(the successor package to `httpx`; same API). Each SDK constructor accepts an
`http_client=` and a `max_retries=`. So:

```python
client = anthropic.Anthropic(http_client=get_http_client(), max_retries=0)
client = openai.OpenAI(http_client=get_http_client(), max_retries=0)
```

`get_http_client()` returns one process-wide `httpx2.Client` whose transport
is `RetryingTransport`. With the SDKs' own retries set to zero, **there is
exactly one retry loop**, and it is ours: tested with a mock transport, logged
through `logger.py`, and configured from `.env`.

For anything outside the SDKs, `request(method, url, **kw)` goes through the
same client and raises on non-2xx.

## The policy

| Aspect | Behaviour |
|---|---|
| Attempts | `HTTP_RETRIES` (default 3) retries after the first try -- up to four requests |
| Retry on | connection errors, connect/read/write/pool timeouts, protocol errors; HTTP 408, 409, 429, 500, 502, 503, 504 |
| Never retry | any other 4xx (400 bad request, 401/403 auth, 404, 422 validation) -- they do not improve by repetition |
| Delay | full-jitter exponential backoff: `uniform(c/2, c)` with `c = min(30, 1·2^attempt)` → ≈1 s, 2 s, 4 s. Jitter spreads simultaneous retries out, which is what a 429 is asking for |
| `Retry-After` | when the server sends one in seconds, it replaces the computed delay |
| Logging | each retry: `http.retry` (WARNING) with method, URL, attempt, max_attempts, wait_s, reason; exhaustion: `http.failed` (ERROR) |
| Failure | `HttpFailure(method, url, attempts, elapsed, status, cause)` -- a one-line message such as `POST https://api.anthropic.com/v1/messages failed after 4 attempt(s) in 7.3s: HTTP 503`. Callers inspect `.status` (HTTP) or `.cause` (network) |
| Timeout | `HTTP_TIMEOUT_SECONDS` (default 60) for connect/read/write/pool |

Both SDKs catch what the transport raised and re-raise it as their own
`APIConnectionError("Connection error.")` with the `HttpFailure` as
`__cause__`. `unwrap_http_failure(exc)` returns that cause, or `None` when
the SDK wrapped something else; `embeddings/openai.py` and
`generation/client.py` both use it so a caller sees the one-line failure
rather than a generic connection error two layers above it.

Streaming responses (the chat answer) pass through unchanged: a retry only
happens before any response body has been handed to the caller.

## Why not the SDK defaults

Both SDKs retry, with their own backoff and their own idea of which statuses
qualify. Layered on top of anything of ours, a single outage would produce
retry storms multiplied across the layers, and the panel could not be told
"this is the retry policy" in one sentence. Owning the transport also means
the retry lines carry the request's trace id, so a slow answer in the demo is
explainable from the log alone.

## Not included, on purpose

Circuit breaking, per-host budgets and response caching. Two upstreams and a
local demo do not need them; each would be a small class on top of a transport
that already sees all the traffic.

## Tests

`tests/test_http_client.py` drives `RetryingTransport` around an
`httpx2.MockTransport` with a scripted upstream and a recorded `sleep`:
503-503-200 succeeds on the third try; connection errors retry; four failures
raise one `HttpFailure` with attempts, status and message; 400/401/403/404/422
are not retried; delays grow 1 → 2 → 4 with `Retry-After` overriding; the cap;
zero retries means one attempt; both SDK clients accept the client with
`max_retries=0`.
