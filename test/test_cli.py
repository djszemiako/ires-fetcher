"""
The command end to end, against a scripted API: docs page, spec, `POST /recalls/` pages
cut from a small fixed dataset, and the by-id `GET /recalls/product/{id}`.
"""

import json

import duckdb
import httpx
import pytest
from typer.testing import CliRunner

from ires_fetch import api, cli
from ires_fetch.cli import app
from ires_fetch.statements import WORKSET_TABLE, sql

PRODUCT_IDS = tuple(str(product_id) for product_id in range(100, 107))

RUNNER = CliRunner()


class _ScriptedApi:
    def __init__(self, docs_html: str, spec_text: bytes):
        self._docs_html = docs_html

        self._spec_text = spec_text

        self.requests: tuple[httpx.Request, ...] = ()

        # Product ids the API refuses; 404 is terminal, so the request fails outright.
        self.failing: frozenset[str] = frozenset()

    def fetched_product_ids(self) -> list[str]:
        return [
            request.url.path.rsplit("/", 1)[-1]
            for request in self.requests
            if "/recalls/product/" in request.url.path
        ]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests = (*self.requests, request)

        path = request.url.path

        if path.endswith("/apidocs/"):
            return httpx.Response(200, text=self._docs_html)

        if path.endswith("/ires.json"):
            return httpx.Response(200, content=self._spec_text)

        if request.headers.get("Authorization-Key") != "k":
            return httpx.Response(401)

        if path == "/rest/iresapi/recalls/":
            payload = json.loads(request.content.decode().removeprefix("payload="))

            start, rows = payload["start"], payload["rows"]

            page = PRODUCT_IDS[start - 1 : start - 1 + rows]

            return httpx.Response(
                200,
                json={
                    "MESSAGE": "success",
                    "RESULTCOUNT": len(PRODUCT_IDS),
                    "STATUSCODE": 400,
                    "RESULT": [{"PRODUCTID": product_id} for product_id in page],
                },
            )

        if path.startswith("/rest/iresapi/recalls/product/"):
            product_id = path.rsplit("/", 1)[-1]

            if product_id in self.failing:
                return httpx.Response(404)

            return httpx.Response(
                200,
                json={
                    "MESSAGE": "success",
                    "RESULTCOUNT": 1,
                    "STATUSCODE": 400,
                    "RESULT": {"COLUMNS": ["PRODUCTID"], "DATA": [[product_id]]},
                },
            )

        return httpx.Response(404)


@pytest.fixture()
def scripted_api(monkeypatch, docs_html, spec_text) -> _ScriptedApi:
    scripted = _ScriptedApi(docs_html, spec_text)

    monkeypatch.setattr(
        cli.http,
        "new_client",
        lambda headers: httpx.Client(
            transport=httpx.MockTransport(scripted), headers=headers
        ),
    )

    monkeypatch.setattr(api.time, "sleep", lambda seconds: None)

    return scripted


def _product_ids_written(directory: str) -> list[int]:
    return [
        path_id
        for (path_id,) in duckdb.sql(
            f"SELECT path_id FROM read_parquet('{directory}/**/*.parquet', "
            f"hive_partitioning = false, union_by_name = true) ORDER BY path_id"
        ).fetchall()
    ]


def _read(directory: str) -> list[tuple]:
    return duckdb.sql(
        f"SELECT request_type, url, start, rows, content "
        f"FROM read_parquet('{directory}/**/*.parquet', hive_partitioning = false) "
        f"ORDER BY start, url"
    ).fetchall()


def test_recalls_run_pages_the_dataset_and_writes_parquet(scripted_api, tmp_path):
    result = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "RECALLS",
            "--dest", str(tmp_path),
            "--authorization-user", "u",
            "--authorization-key", "k",
            "--rows", "3",
            "--max-workers", "2",
        ),
    )  # fmt: skip

    assert result.exit_code == 0, result.output

    rows = _read(str(tmp_path / "raw/api_responses/recalls/run_id=r1"))

    assert [(start, size) for _, _, start, size, _ in rows] == [(1, 3), (4, 3), (7, 3)]

    fetched = [
        record["PRODUCTID"]
        for *_, content in rows
        for record in json.loads(content)["RESULT"]
    ]

    assert fetched == list(PRODUCT_IDS)

    posted = [r for r in scripted_api.requests if r.method == "POST"]

    # One count probe plus three pages, every one with the documented columns.
    assert len(posted) == 4

    page_payload = json.loads(posted[-1].content.decode().removeprefix("payload="))

    assert page_payload["displaycolumns"].startswith("productid,recalleventid,")

    assert page_payload["filter"] == "[{'centercd':['CBER','CDER']}]"


def test_by_id_run_draws_its_ids_from_a_recalls_run(scripted_api, tmp_path):
    recalls = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "RECALLS",
            "--dest", str(tmp_path),
            "--authorization-user", "u",
            "--authorization-key", "k",
            "--rows", "5",
        ),
    )  # fmt: skip

    assert recalls.exit_code == 0, recalls.output

    products = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "PRODUCT",
            "--dest", str(tmp_path),
            "--ids-source", str(tmp_path / "raw/api_responses/recalls/run_id=r1"),
            "--authorization-user", "u",
            "--authorization-key", "k",
            "--limit", "4",
        ),
    )  # fmt: skip

    assert products.exit_code == 0, products.output

    rows = _read(str(tmp_path / "raw/api_responses/product/run_id=r1"))

    assert [url.rsplit("/", 1)[-1] for _, url, *_ in rows] == list(PRODUCT_IDS[:4])

    assert all(start is None and size is None for _, _, start, size, _ in rows)


def test_by_id_run_requires_an_ids_source(scripted_api, tmp_path):
    result = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "PRODUCT",
            "--dest", str(tmp_path),
            "--authorization-user", "u",
            "--authorization-key", "k",
        ),
    )  # fmt: skip

    assert result.exit_code != 0

    assert "--ids-source is required" in result.output


def test_credentials_come_from_the_environment(scripted_api, tmp_path, monkeypatch):
    monkeypatch.setenv("IRES_AUTHORIZATION_USER", "u")

    monkeypatch.setenv("IRES_AUTHORIZATION_KEY", "k")

    result = RUNNER.invoke(
        app,
        ("--run-id", "r1", "--request-type", "PRODUCT_TYPES", "--dest", str(tmp_path)),
    )

    # The scripted API has no product types route, so the single request 404s and the
    # run reports it - after writing the (empty) parquet.
    assert result.exit_code != 0

    assert isinstance(result.exception, cli.IncompleteFetchError)


def _recalls_run(tmp_path) -> None:
    result = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "RECALLS",
            "--dest", str(tmp_path),
            "--authorization-user", "u",
            "--authorization-key", "k",
            "--rows", "5",
        ),
    )  # fmt: skip

    assert result.exit_code == 0, result.output


def _product_run(tmp_path, *options: str):
    return RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "PRODUCT",
            "--dest", str(tmp_path),
            "--ids-source", str(tmp_path / "raw/api_responses/recalls/run_id=r1"),
            "--authorization-user", "u",
            "--authorization-key", "k",
            *options,
        ),
    )  # fmt: skip


def test_a_by_id_run_resumes_where_the_partition_left_off(scripted_api, tmp_path):
    _recalls_run(tmp_path)

    first = _product_run(tmp_path, "--limit", "4")

    assert first.exit_code == 0, first.output

    scripted_api.requests = ()

    second = _product_run(tmp_path)

    assert second.exit_code == 0, second.output

    # Only the ids the first run left behind are asked for the second time round.
    assert scripted_api.fetched_product_ids() == list(PRODUCT_IDS[4:])

    directory = str(tmp_path / "raw/api_responses/product/run_id=r1")

    assert _product_ids_written(directory) == [int(one) for one in PRODUCT_IDS]

    scripted_api.requests = ()

    third = _product_run(tmp_path)

    assert third.exit_code == 0, third.output

    # A complete partition leaves nothing to fetch and nothing to write.
    assert scripted_api.fetched_product_ids() == []

    assert _product_ids_written(directory) == [int(one) for one in PRODUCT_IDS]


def test_force_fetches_the_whole_workset_again(scripted_api, tmp_path):
    _recalls_run(tmp_path)

    assert _product_run(tmp_path, "--limit", "2").exit_code == 0

    forced = _product_run(tmp_path, "--limit", "2", "--force")

    assert forced.exit_code == 0, forced.output

    assert scripted_api.fetched_product_ids()[-2:] == list(PRODUCT_IDS[:2])

    # Runs append: the refetched ids are written a second time rather than replacing.
    directory = str(tmp_path / "raw/api_responses/product/run_id=r1")

    assert _product_ids_written(directory) == [100, 100, 101, 101]


def test_a_failed_request_is_picked_up_by_the_next_run(scripted_api, tmp_path):
    _recalls_run(tmp_path)

    scripted_api.failing = frozenset({"103"})

    failing = _product_run(tmp_path, "--batch-size", "2")

    assert isinstance(failing.exception, cli.IncompleteFetchError)

    directory = str(tmp_path / "raw/api_responses/product/run_id=r1")

    # Every batch that ran is on disk, the failed id apart.
    assert _product_ids_written(directory) == [
        int(one) for one in PRODUCT_IDS if one != "103"
    ]

    scripted_api.failing = frozenset()

    scripted_api.requests = ()

    resumed = _product_run(tmp_path)

    assert resumed.exit_code == 0, resumed.output

    assert scripted_api.fetched_product_ids() == ["103"]

    assert _product_ids_written(directory) == [int(one) for one in PRODUCT_IDS]


def test_each_batch_is_written_as_it_finishes(scripted_api, tmp_path):
    result = RUNNER.invoke(
        app,
        (
            "--run-id", "r1",
            "--request-type", "RECALLS",
            "--dest", str(tmp_path),
            "--authorization-user", "u",
            "--authorization-key", "k",
            "--rows", "3",
            "--batch-size", "1",
        ),
    )  # fmt: skip

    assert result.exit_code == 0, result.output

    directory = tmp_path / "raw/api_responses/recalls/run_id=r1"

    # One write per batch, appended: three pages, three batches, three files.
    assert len(list(directory.rglob("*.parquet"))) == 3

    rows = _read(str(directory))

    assert [(start, size) for _, _, start, size, _ in rows] == [(1, 3), (4, 3), (7, 3)]


def test_ids_are_pulled_a_batch_at_a_time_while_the_run_writes():
    connection = duckdb.connect(":memory:")

    connection.execute(
        f"CREATE TABLE {WORKSET_TABLE} AS SELECT range AS id FROM range(5)"
    )

    pulled = []

    for path_id in cli._stream_ids(connection, "workset.all_ids", 2):
        pulled.append(path_id)

        # What the batch loop does between pulls; on the run's own connection this
        # replaces the open result set and the stream reads back the DDL's row count.
        connection.execute(sql("responses.ddl"))

    assert pulled == [0, 1, 2, 3, 4]
