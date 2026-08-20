"""
Polite, retrying HTTP over httpx.

One `httpx.Client` per worker thread: a client owns a cookie jar, and Akamai's bot manager
(`_abck`, `bm_sz`) ripens a jar over a few round trips from the same client, so a worker
sticks to its own. Transient answers and transport errors are retried with exponential
backoff and jitter; anything else propagates.
"""

import logging
import random
import time
from itertools import count
from typing import NamedTuple, Optional

import httpx

from ires_fetch.constants import REQUEST_TIMEOUT_SECONDS

logger = logging.getLogger(__name__)

# 403 is included because accessdata.fda.gov answers it as a bot-manager challenge that
# clears once the client's cookies have ripened, not as a terminal denial.
RETRIABLE_STATUSES: tuple[int, ...] = (403, 408, 425, 429, 500, 502, 503, 504)


class RetryPolicy(NamedTuple):
    max_tries: int = 10
    min_delay_seconds: float = 1.0
    max_delay_seconds: float = 30.0
    smoothness_factor: float = 1.5


DEFAULT_RETRY_POLICY = RetryPolicy()


class ExhaustedRetriesError(Exception):
    def __init__(self, method: str, url: str, status: Optional[int]):
        super().__init__(f"{method} {url}: ran out of retries (last status {status})")

        self.method = method

        self.url = url

        self.status = status


def new_client(headers: dict[str, str]) -> httpx.Client:
    """
    A client with its own cookie jar and the run's base headers.

    Args:
        headers: Headers sent on every request of the client

    Returns:
        An open client; the caller closes it
    """
    return httpx.Client(
        headers=headers, timeout=REQUEST_TIMEOUT_SECONDS, follow_redirects=True
    )


def backoff_seconds(policy: RetryPolicy, tries: int) -> float:
    """
    The pause before retry number `tries`: exponential, clamped, plus up to a second of
    jitter so workers that failed together do not retry together.
    """
    delay = min(
        max(
            policy.min_delay_seconds * policy.smoothness_factor**tries,
            policy.min_delay_seconds,
        ),
        policy.max_delay_seconds,
    )

    return delay + random.random()


def responsible_request(
    client: httpx.Client,
    method: str,
    url: str,
    policy: RetryPolicy = DEFAULT_RETRY_POLICY,
    **kwargs,
) -> httpx.Response:
    """
    Make one request, retrying transient failures.

    Args:
        client: The worker's client
        method: HTTP method
        url: Absolute URL
        policy: How often and how patiently to retry
        **kwargs: Passed to `httpx.Client.request`, e.g. `headers`, `content`

    Returns:
        A response with a non-retriable status below 400
    """
    last_status: Optional[int] = None

    for tries in count(1):
        try:
            response = client.request(method, url, **kwargs)
        except httpx.TransportError as error:
            logger.warning("%s %s: %s", method, url, error)
        else:
            if response.status_code not in RETRIABLE_STATUSES:
                response.raise_for_status()

                return response

            last_status = response.status_code

            logger.warning("%s %s: HTTP %s", method, url, last_status)

        if tries >= policy.max_tries:
            raise ExhaustedRetriesError(method, url, last_status)

        pause = backoff_seconds(policy, tries)

        logger.info("Retrying %s %s in %.1fs", method, url, pause)

        time.sleep(pause)

    raise AssertionError("unreachable")
