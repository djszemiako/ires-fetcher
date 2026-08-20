import hashlib
import json
from datetime import UTC, datetime

import httpx
import pytest

from ires_fetch import api
from ires_fetch.api import (
    IresApiError,
    IresCredentials,
    IresRequest,
    build_payload,
    build_url,
    count_recalls,
    fetch,
    fetch_all,
    format_filters,
    plan_id_requests,
    plan_recall_pages,
    plan_single_request,
    rows_per_page,
)
from ires_fetch.constants import API_BASE_URL, DEFAULT_FILTERS, IresRequestTypes

CREDENTIALS = IresCredentials("user@example.com", "secret")

FILTERS = list(DEFAULT_FILTERS)


class _Recorder:
    """A MockTransport handler that answers with a fixed body and keeps the request."""

    def __init__(self, body: dict, headers: dict[str, str] | None = None):
        self._content = json.dumps(body).encode("utf-8")

        self._headers = headers or {}

        self.requests: tuple[httpx.Request, ...] = ()

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests = (*self.requests, request)

        return httpx.Response(
            200,
            content=self._content,
            headers={"Content-Type": "application/json;charset=UTF-8", **self._headers},
            request=request,
        )

    @property
    def last(self) -> httpx.Request:
        return self.requests[-1]


def _client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _recalls_request(start: int = 1, rows: int = 2500) -> IresRequest:
    return IresRequest(
        request_type=IresRequestTypes.RECALLS,
        url=build_url(IresRequestTypes.RECALLS),
        path_id=None,
        display_columns=["productid", "rid"],
        filters=FILTERS,
        sort="productid",
        sort_order="asc",
        start=start,
        rows=rows,
    )


def test_format_filters_uses_single_quotes_and_no_spaces():
    assert format_filters(FILTERS) == "[{'centercd':['CBER','CDER']}]"


def test_build_payload_is_a_form_field_holding_json():
    body = build_payload(_recalls_request(start=2501))

    assert body.startswith("payload=")

    assert json.loads(body.removeprefix("payload=")) == {
        "displaycolumns": "productid,rid",
        "filter": "[{'centercd':['CBER','CDER']}]",
        "start": 2501,
        "rows": 2500,
        "sort": "productid",
        "sortorder": "asc",
    }


@pytest.mark.parametrize(
    "request_type, path_id, expected",
    (
        (IresRequestTypes.RECALLS, None, f"{API_BASE_URL}/recalls/"),
        (IresRequestTypes.PRODUCT_TYPES, None, f"{API_BASE_URL}/search/producttypes"),
        (IresRequestTypes.PRODUCT, 100063, f"{API_BASE_URL}/recalls/product/100063"),
        (IresRequestTypes.EVENT, 58700, f"{API_BASE_URL}/recalls/event/58700"),
        (
            IresRequestTypes.PRESS_RELEASE_URLS,
            1,
            f"{API_BASE_URL}/search/pressreleaseurls/1",
        ),
    ),
)
def test_build_url_fills_the_spec_placeholder(request_type, path_id, expected):
    assert build_url(request_type, path_id) == expected


def test_build_url_requires_an_id_for_by_id_endpoints():
    with pytest.raises(ValueError, match="requires a path id"):
        build_url(IresRequestTypes.CODE_INFO)


@pytest.mark.parametrize(
    "columns, expected",
    (
        (["productid", "rid"], 5000),
        ([], 5000),
        (["productid", "codeinformation"], 2500),
    ),
)
def test_rows_per_page_halves_once_code_information_is_requested(columns, expected):
    assert rows_per_page(columns) == expected


@pytest.mark.parametrize(
    "total, rows, starts",
    (
        (49096, 2500, tuple(range(1, 49097, 2500))),
        (2500, 2500, (1,)),
        (2501, 2500, (1, 2501)),
        (0, 2500, ()),
    ),
)
def test_plan_recall_pages_steps_by_record_offset(total, rows, starts):
    pages = list(plan_recall_pages(total, ["productid"], rows, FILTERS))

    assert tuple(page.start for page in pages) == starts

    assert all(page.rows == rows for page in pages)

    assert all(page.filters == FILTERS for page in pages)

    assert all(page.sort == "productid" and page.sort_order == "asc" for page in pages)


def test_plan_id_requests_carry_no_payload():
    requests = list(plan_id_requests(IresRequestTypes.PRODUCT, ["productid"], (1, 2)))

    assert [request.url for request in requests] == [
        f"{API_BASE_URL}/recalls/product/1",
        f"{API_BASE_URL}/recalls/product/2",
    ]

    assert all(
        (request.filters, request.sort, request.sort_order, request.start, request.rows)
        == (None, None, None, None, None)
        for request in requests
    )

    # The id the url was built from is kept, and is what a resumed run skips on.
    assert [request.path_id for request in requests] == [1, 2]


def test_plan_single_request_is_one_request():
    requests = list(plan_single_request(IresRequestTypes.PRODUCT_TYPES, ["centercd"]))

    assert [request.url for request in requests] == [
        f"{API_BASE_URL}/search/producttypes"
    ]

    assert requests[0].path_id is None


def test_fetch_posts_the_payload_and_records_the_response():
    body = {"MESSAGE": "success", "RESULTCOUNT": 2, "RESULT": [{"PRODUCTID": "1"}]}

    recorder = _Recorder(body, {"Content-Length": "999"})

    request = _recalls_request()

    with _client(recorder) as client:
        response = fetch(client, request, CREDENTIALS)

    sent = recorder.last

    assert sent.method == "POST"

    assert str(sent.url) == f"{request.url}?signature={response.signature}"

    assert response.signature.isdigit()

    assert sent.headers["Authorization-User"] == CREDENTIALS.user

    assert sent.headers["Authorization-Key"] == CREDENTIALS.key

    assert sent.headers["Content-Type"] == "application/x-www-form-urlencoded"

    assert sent.content.decode() == build_payload(request)

    assert response.request_type == "RECALLS"

    assert response.url == request.url

    assert response.display_columns == ["productid", "rid"]

    assert response.filters == FILTERS

    assert (response.start, response.rows) == (1, 2500)

    assert response.content_type == "application/json;charset=UTF-8"

    assert response.content_length == 999

    assert response.content == json.dumps(body)

    assert response.sha_256 == hashlib.sha256(json.dumps(body).encode()).hexdigest()

    assert response.fetched_at.tzinfo == UTC

    assert response.fetched_at <= datetime.now(UTC)


def test_fetch_gets_by_id_endpoints_without_a_body():
    recorder = _Recorder({"MESSAGE": "success", "RESULT": {"COLUMNS": [], "DATA": []}})

    request = next(plan_id_requests(IresRequestTypes.EVENT, ["recalleventid"], (5,)))

    with _client(recorder) as client:
        response = fetch(client, request, CREDENTIALS)

    assert recorder.last.method == "GET"

    assert recorder.last.content == b""

    assert "Content-Type" not in recorder.last.headers

    assert response.content_length == len(response.content.encode("utf-8"))

    assert response.filters is None

    assert response.path_id == 5


def test_fetch_raises_on_an_application_level_failure():
    recorder = _Recorder(
        {"MESSAGE": "The payload rows should be less than 2500", "STATUSCODE": 417}
    )

    with (
        _client(recorder) as client,
        pytest.raises(IresApiError, match="rows should be less than 2500"),
    ):
        fetch(client, _recalls_request(rows=5000), CREDENTIALS)


def test_count_recalls_reads_resultcount_from_a_one_row_probe():
    recorder = _Recorder(
        {"MESSAGE": "success", "RESULTCOUNT": 49096, "RESULT": [{"PRODUCTID": "1"}]}
    )

    with _client(recorder) as client:
        assert count_recalls(client, CREDENTIALS, FILTERS) == 49096

    payload = json.loads(recorder.last.content.decode().removeprefix("payload="))

    assert (payload["rows"], payload["start"], payload["displaycolumns"]) == (1, 1, "")


def test_fetch_all_yields_every_outcome_and_closes_the_clients(monkeypatch):
    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)

    def _handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode().removeprefix("payload="))

        if payload["start"] == 2501:
            return httpx.Response(
                200, json={"MESSAGE": "boom", "STATUSCODE": 417}, request=request
            )

        return httpx.Response(
            200,
            json={"MESSAGE": "success", "RESULT": [{"PRODUCTID": "1"}]},
            request=request,
        )

    clients: list[httpx.Client] = []

    def _new_client(headers: dict[str, str]) -> httpx.Client:
        client = httpx.Client(transport=httpx.MockTransport(_handler), headers=headers)

        clients.append(client)

        return client

    monkeypatch.setattr(api.http, "new_client", _new_client)

    requests = list(plan_recall_pages(7500, ["productid"], 2500, FILTERS))

    outcomes = list(fetch_all(requests, CREDENTIALS, 2))

    assert len(outcomes) == 3

    succeeded = {
        outcome.request.start for outcome in outcomes if outcome.response is not None
    }

    failed = [outcome for outcome in outcomes if outcome.error is not None]

    assert succeeded == {1, 5001}

    assert [outcome.request.start for outcome in failed] == [2501]

    assert isinstance(failed[0].error, IresApiError)

    assert 1 <= len(clients) <= 2

    assert all(client.is_closed for client in clients)
