import httpx
import pytest

from ires_fetch import http
from ires_fetch.http import (
    ExhaustedRetriesError,
    RetryPolicy,
    backoff_seconds,
    responsible_request,
)


class _Answers:
    """Hands out scripted responses in order and counts the calls."""

    def __init__(self, *answers):
        self._answers = answers

        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        answer = self._answers[min(self.calls, len(self._answers) - 1)]

        self.calls += 1

        if isinstance(answer, Exception):
            raise answer

        return httpx.Response(answer, text="body", request=request)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda seconds: None)


def _client(answers: _Answers) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(answers))


def test_backoff_grows_and_is_clamped():
    policy = RetryPolicy(max_delay_seconds=5.0, smoothness_factor=2.0)

    first = backoff_seconds(policy, 1)

    second = backoff_seconds(policy, 2)

    tenth = backoff_seconds(policy, 10)

    assert 2.0 <= first < 3.0

    assert 4.0 <= second < 5.0

    assert 5.0 <= tenth < 6.0


def test_responsible_request_returns_the_first_good_answer():
    answers = _Answers(200)

    with _client(answers) as client:
        response = responsible_request(client, "GET", "https://example.test/")

    assert response.status_code == 200

    assert answers.calls == 1


@pytest.mark.parametrize("transient", (403, 429, 503, httpx.ConnectError("refused")))
def test_responsible_request_retries_transient_failures(transient):
    answers = _Answers(transient, transient, 200)

    with _client(answers) as client:
        response = responsible_request(client, "GET", "https://example.test/")

    assert response.status_code == 200

    assert answers.calls == 3


def test_responsible_request_gives_up_after_max_tries():
    answers = _Answers(503)

    with _client(answers) as client, pytest.raises(ExhaustedRetriesError) as raised:
        responsible_request(
            client, "GET", "https://example.test/", RetryPolicy(max_tries=4)
        )

    assert answers.calls == 4

    assert raised.value.status == 503


def test_responsible_request_raises_terminal_statuses_at_once():
    answers = _Answers(404)

    with _client(answers) as client, pytest.raises(httpx.HTTPStatusError):
        responsible_request(client, "GET", "https://example.test/")

    assert answers.calls == 1
