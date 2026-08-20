"""
Requests to the iRES API, and the verbatim record kept of each response.

A run fetches one endpoint. `POST /recalls/` is paged by record offset after a one-row
probe reports the filtered total; the by-id `GET` endpoints take one request per id drawn
from a prior `RECALLS` run. Either way the requests are planned lazily and handed to
`fetch_all` a batch at a time by a pool of workers, each with its own client, and every
response is kept as the raw JSON text it arrived as.
"""

import hashlib
import json
import logging
import random
import threading
import time
from collections.abc import Generator, Iterable
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import Literal, NamedTuple, Optional

import httpx

from ires_fetch import http
from ires_fetch.constants import (
    API_BASE_URL,
    API_REQUEST_HEADERS,
    AUTHORIZATION_KEY_HEADER,
    AUTHORIZATION_USER_HEADER,
    CODE_INFORMATION_COLUMN,
    DEFAULT_SORT,
    DEFAULT_SORT_ORDER,
    FETCH_COOLDOWN_SECONDS,
    FIRST_RECORD_OFFSET,
    MAX_ROWS_PER_PAGE,
    MAX_ROWS_PER_PAGE_WITH_CODE_INFORMATION,
    POST_CONTENT_TYPE,
    SUCCESS_MESSAGE,
    IresRequestTypes,
)

logger = logging.getLogger(__name__)

FiltersType = list[dict[str, list[str]]]


class IresCredentials(NamedTuple):
    user: str
    key: str


class IresRequest(NamedTuple):
    """
    One planned call. `url` excludes the cache-busting signature, which is minted at fetch
    time. The payload fields are `None` for endpoints that take no payload; `path_id` is
    the id the url was built from, and is what a resumed run anti-joins against.
    """

    request_type: IresRequestTypes
    url: str
    path_id: Optional[int]
    display_columns: list[str]
    filters: Optional[FiltersType]
    sort: Optional[str]
    sort_order: Optional[Literal["asc"]]
    start: Optional[int]
    rows: Optional[int]


class IresResponse(NamedTuple):
    """
    The record kept of one call: what was asked, what came back, and when. `content` is
    the response body verbatim; nothing is parsed out of it beyond the success check.
    """

    request_type: str
    url: str
    path_id: Optional[int]
    signature: str
    display_columns: list[str]
    filters: Optional[FiltersType]
    sort: Optional[str]
    sort_order: Optional[Literal["asc"]]
    start: Optional[int]
    rows: Optional[int]
    content_type: str
    content_length: int
    sha_256: str
    fetched_at: datetime
    content: str


class IresApiError(Exception):
    """
    The API answered HTTP 200 but its body reports a failure.
    """

    def __init__(self, url: str, message: str, status_code: Optional[int]):
        super().__init__(
            f"iRES API error for {url}: {message} (STATUSCODE {status_code})"
        )

        self.url = url

        self.message = message

        self.status_code = status_code


def format_filters(filters: FiltersType) -> str:
    """
    Spell filters the way the payload wants them: a JSON-shaped array with single quotes,
    e.g. `[{'centercd':['CBER','CDER']}]`.

    Args:
        filters: One single-key mapping per filter

    Returns:
        The `filter` payload value
    """
    return json.dumps(filters, separators=(",", ":")).replace('"', "'")


def build_payload(request: IresRequest) -> str:
    """
    Build the form-encoded body of a `POST /recalls/` call.

    Args:
        request: A planned call to an endpoint that accepts a payload

    Returns:
        The request body, `payload={...}`
    """
    payload = {
        "displaycolumns": ",".join(request.display_columns),
        "filter": format_filters(request.filters or []),
        "start": request.start,
        "rows": request.rows,
        "sort": request.sort,
        "sortorder": request.sort_order,
    }

    return f"payload={json.dumps(payload)}"


def build_url(request_type: IresRequestTypes, path_id: Optional[int] = None) -> str:
    """
    Resolve an endpoint's URL, filling the spec's `{eventid}` / `{productid}` placeholder.

    Args:
        request_type: Endpoint to address
        path_id: Value for the placeholder; required by the by-id endpoints

    Returns:
        Absolute URL without the signature
    """
    if request_type.needs_ids and path_id is None:
        raise ValueError(f"{request_type.name} requires a path id")

    path = request_type.path

    if path_id is not None:
        path = path[: path.index("{")] + str(path_id)

    return f"{API_BASE_URL}{path}"


def rows_per_page(display_columns: Iterable[str]) -> int:
    """
    The largest page the API serves for a column selection.

    Args:
        display_columns: Columns the page will request

    Returns:
        Rows per page
    """
    if CODE_INFORMATION_COLUMN in display_columns:
        return MAX_ROWS_PER_PAGE_WITH_CODE_INFORMATION

    return MAX_ROWS_PER_PAGE


def new_signature() -> str:
    """
    The cache-busting query value the documentation prescribes: epoch seconds.
    """
    return str(int(datetime.now(UTC).timestamp()))


def plan_recall_pages(
    total: int,
    display_columns: list[str],
    rows: int,
    filters: Optional[FiltersType],
    sort: str = DEFAULT_SORT,
    sort_order: Literal["asc"] = DEFAULT_SORT_ORDER,
) -> Generator[IresRequest]:
    """
    Plan the `POST /recalls/` pages that together cover every filtered record.

    Args:
        total: `RESULTCOUNT` reported for the filters
        display_columns: Columns every page requests
        rows: Rows per page
        filters: Filters every page applies
        sort: Column the pages are ordered by; what makes offset paging stable
        sort_order: Direction of that order

    Returns:
        Generator of one request per page, in offset order
    """
    url = build_url(IresRequestTypes.RECALLS)

    for start in range(FIRST_RECORD_OFFSET, total + FIRST_RECORD_OFFSET, rows):
        yield IresRequest(
            request_type=IresRequestTypes.RECALLS,
            url=url,
            path_id=None,
            display_columns=display_columns,
            filters=filters,
            sort=sort,
            sort_order=sort_order,
            start=start,
            rows=rows,
        )


def plan_id_requests(
    request_type: IresRequestTypes,
    display_columns: list[str],
    path_ids: Iterable[int],
) -> Generator[IresRequest]:
    """
    Plan one call per id to a by-id `GET` endpoint.

    Args:
        request_type: A by-id endpoint
        display_columns: Columns the endpoint documents itself as exporting
        path_ids: Values for the endpoint's path placeholder

    Returns:
        Generator of one request per id
    """
    for path_id in path_ids:
        yield IresRequest(
            request_type=request_type,
            url=build_url(request_type, path_id),
            path_id=path_id,
            display_columns=display_columns,
            filters=None,
            sort=None,
            sort_order=None,
            start=None,
            rows=None,
        )


def plan_single_request(
    request_type: IresRequestTypes, display_columns: list[str]
) -> Generator[IresRequest]:
    """
    Plan the one call a parameterless `GET` endpoint takes.

    Args:
        request_type: An endpoint that takes neither payload nor path id
        display_columns: Columns the endpoint documents itself as exporting

    Returns:
        Generator of the single request
    """
    yield IresRequest(
        request_type=request_type,
        url=build_url(request_type),
        path_id=None,
        display_columns=display_columns,
        filters=None,
        sort=None,
        sort_order=None,
        start=None,
        rows=None,
    )


def _request_headers(
    request: IresRequest, credentials: IresCredentials
) -> dict[str, str]:
    headers = {
        AUTHORIZATION_USER_HEADER: credentials.user,
        AUTHORIZATION_KEY_HEADER: credentials.key,
    }

    if request.request_type.accepts_payload:
        headers["Content-Type"] = POST_CONTENT_TYPE

    return headers


def _assert_success(url: str, content: str) -> None:
    body = json.loads(content)

    message = body.get("MESSAGE")

    if message != SUCCESS_MESSAGE:
        raise IresApiError(url, str(message), body.get("STATUSCODE"))


def fetch(
    client: httpx.Client, request: IresRequest, credentials: IresCredentials
) -> IresResponse:
    """
    Make one call and record it.

    `responsible_request` supplies backoff and retries; the client's cookie jar is what
    Akamai's bot manager keys on. A well-formed call is always HTTP 200; the body's
    `MESSAGE` decides whether it is a success.

    Args:
        client: The worker's client, carrying the API's base headers
        request: The planned call
        credentials: API credentials sent as headers

    Returns:
        The record of the call
    """
    signature = new_signature()

    url = f"{request.url}?signature={signature}"

    headers = _request_headers(request, credentials)

    if request.request_type.accepts_payload:
        response = http.responsible_request(
            client, "POST", url, headers=headers, content=build_payload(request)
        )
    else:
        response = http.responsible_request(client, "GET", url, headers=headers)

    fetched_at = datetime.now(UTC)

    raw = response.content

    content = raw.decode("utf-8")

    _assert_success(request.url, content)

    # Gzip-encoded answers are chunked and carry no `Content-Length`; the decoded body's
    # size stands in for it then.
    content_length = int(response.headers.get("Content-Length") or len(raw))

    return IresResponse(
        request_type=request.request_type.name,
        url=request.url,
        path_id=request.path_id,
        signature=signature,
        display_columns=request.display_columns,
        filters=request.filters,
        sort=request.sort,
        sort_order=request.sort_order,
        start=request.start,
        rows=request.rows,
        content_type=response.headers.get("Content-Type", ""),
        content_length=content_length,
        sha_256=hashlib.sha256(raw).hexdigest(),
        fetched_at=fetched_at,
        content=content,
    )


def count_recalls(
    client: httpx.Client,
    credentials: IresCredentials,
    filters: Optional[FiltersType],
) -> int:
    """
    Ask `POST /recalls/` how many records the filters match.

    A one-row, no-column page answers with the full `RESULTCOUNT`.

    Args:
        client: Client the call goes through
        credentials: API credentials
        filters: Filters the count is for

    Returns:
        `RESULTCOUNT`
    """
    probe = IresRequest(
        request_type=IresRequestTypes.RECALLS,
        url=build_url(IresRequestTypes.RECALLS),
        path_id=None,
        display_columns=[],
        filters=filters,
        sort=DEFAULT_SORT,
        sort_order=DEFAULT_SORT_ORDER,
        start=FIRST_RECORD_OFFSET,
        rows=1,
    )

    response = fetch(client, probe, credentials)

    return int(json.loads(response.content)["RESULTCOUNT"])


class WorkerClients:
    """
    One `httpx.Client` per executor thread.

    Threads never share a client: a client's cookie jar ripens fastest when one worker
    sticks to it, and the jar is what the host's bot manager keys on. Clients are created
    by the executor's `initializer` and closed by `cleanup`.
    """

    def __init__(self, headers: dict[str, str]):
        self._headers = headers

        self._clients: dict[int, httpx.Client] = {}

        self._lock = threading.Lock()

        self._local = threading.local()

    def initialize(self) -> None:
        client = http.new_client(self._headers)

        self._local.client = client

        with self._lock:
            self._clients[threading.get_ident()] = client

    @property
    def client(self) -> httpx.Client:
        return self._local.client

    def cleanup(self) -> None:
        with self._lock:
            for client in self._clients.values():
                client.close()

            self._clients = {}


class FetchOutcome(NamedTuple):
    request: IresRequest
    response: Optional[IresResponse]
    error: Optional[BaseException]


def fetch_all(
    requests: Iterable[IresRequest],
    credentials: IresCredentials,
    max_workers: int,
    cooldown_seconds: tuple[float, float] = FETCH_COOLDOWN_SECONDS,
) -> Generator[FetchOutcome]:
    """
    Fetch every planned request through a bounded worker pool, one client per worker.

    Each worker sleeps through the politeness window before every fetch. Outcomes are
    yielded as they complete, failures included, so the caller keeps every success and
    decides what a failure costs the run.

    Args:
        requests: The planned calls
        credentials: API credentials
        max_workers: Concurrent workers, and so concurrent clients
        cooldown_seconds: Bounds of the politeness window

    Returns:
        Generator of one outcome per request, in completion order
    """
    clients = WorkerClients(API_REQUEST_HEADERS)

    def _fetch_politely(request: IresRequest) -> IresResponse:
        time.sleep(random.uniform(*cooldown_seconds))

        return fetch(clients.client, request, credentials)

    try:
        with ThreadPoolExecutor(
            max_workers=max_workers, initializer=clients.initialize
        ) as executor:
            futures: dict[Future[IresResponse], IresRequest] = {
                executor.submit(_fetch_politely, request): request
                for request in requests
            }

            for future in as_completed(futures):
                request = futures[future]

                error = future.exception()

                if error is not None:
                    logger.error(
                        "Request failed: %s start=%s: %s",
                        request.url,
                        request.start,
                        error,
                    )

                    yield FetchOutcome(request, None, error)

                    continue

                yield FetchOutcome(request, future.result(), None)
    finally:
        clients.cleanup()
