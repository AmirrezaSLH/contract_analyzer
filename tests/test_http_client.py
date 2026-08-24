"""http_client.py: the retry policy, exercised with a MockTransport and no sleeping."""

from __future__ import annotations

import httpx2 as httpx
import pytest

from contract_analyzer import http_client as H


class Script:
    """A scripted upstream: each call pops the next outcome (status int or exception)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        headers = {}
        if isinstance(outcome, tuple):
            outcome, headers = outcome
        return httpx.Response(outcome, headers=headers, json={"ok": outcome < 400})


def client(script: Script, retries: int = 3, sleeps: list[float] | None = None) -> httpx.Client:
    return httpx.Client(
        transport=H.RetryingTransport(
            httpx.MockTransport(script),
            retries=retries,
            sleep=(sleeps.append if sleeps is not None else lambda s: None),
        )
    )


def test_two_503s_then_200_succeeds_on_the_third_attempt():
    script = Script(503, 503, 200)
    response = client(script).get("https://api.example/v1")
    assert response.status_code == 200 and script.calls == 3


def test_connection_errors_are_retried_too():
    script = Script(httpx.ConnectError("refused"), httpx.ReadTimeout("slow"), 200)
    assert client(script).get("https://api.example/v1").status_code == 200
    assert script.calls == 3


def test_exhausted_retries_raise_one_http_failure_with_the_facts():
    script = Script(503, 503, 503, 503)
    with pytest.raises(H.HttpFailure) as info:
        client(script).post("https://api.example/v1/messages")
    failure = info.value
    assert script.calls == 4  # first try + 3 retries
    assert failure.attempts == 4 and failure.status == 503 and failure.cause is None
    assert "POST https://api.example/v1/messages failed after 4 attempt(s)" in str(failure)
    assert "HTTP 503" in str(failure)


def test_exhausted_network_errors_name_the_cause():
    script = Script(*[httpx.ConnectError("refused")] * 4)
    with pytest.raises(H.HttpFailure) as info:
        client(script).get("https://api.example/")
    assert info.value.status is None
    assert isinstance(info.value.cause, httpx.ConnectError)
    assert "ConnectError" in str(info.value)


def test_client_errors_are_not_retried():
    script = Script(400, 200)
    assert client(script).get("https://api.example/").status_code == 400
    assert script.calls == 1


@pytest.mark.parametrize("status", [401, 403, 404, 422])
def test_auth_and_validation_errors_are_never_retried(status):
    script = Script(status, 200)
    assert client(script).get("https://api.example/").status_code == status
    assert script.calls == 1


def test_backoff_grows_exponentially_and_honours_retry_after():
    sleeps: list[float] = []
    script = Script(429, (429, {"retry-after": "7"}), 503, 200)
    client(script, sleeps=sleeps).get("https://api.example/")
    assert len(sleeps) == 3
    assert 0.5 <= sleeps[0] <= 1.0          # attempt 0: uniform(0.5, 1)
    assert sleeps[1] == 7.0                 # Retry-After wins over the computed delay
    assert 2.0 <= sleeps[2] <= 4.0          # attempt 2: uniform(2, 4)


def test_backoff_delay_is_capped():
    assert H.backoff_delay(20) <= H.MAX_BACKOFF


def test_zero_retries_means_one_attempt():
    script = Script(503, 200)
    with pytest.raises(H.HttpFailure):
        client(script, retries=0).get("https://api.example/")
    assert script.calls == 1


def test_build_http_client_wraps_the_given_inner_transport():
    script = Script(502, 200)
    c = H.build_http_client(transport=httpx.MockTransport(script), retries=1, backoff_base=0.0)
    assert c.get("https://api.example/").status_code == 200


def test_sdk_clients_can_be_built_on_it():
    """The point of the module: both SDKs accept the client and have their own retries off."""
    anthropic = pytest.importorskip("anthropic")
    openai = pytest.importorskip("openai")
    c = H.build_http_client(transport=httpx.MockTransport(Script(200)))
    a = anthropic.Anthropic(api_key="x", http_client=c, max_retries=0)
    o = openai.OpenAI(api_key="x", http_client=c, max_retries=0)
    assert a.max_retries == 0 and o.max_retries == 0
