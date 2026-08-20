import json
from datetime import UTC, datetime

import duckdb
import pytest

from ires_fetch.api import IresResponse
from ires_fetch.statements import (
    FETCHED_RESPONSES_VIEW,
    RECALLS_SOURCE_VIEW,
    RESPONSES_TABLE,
    WORKSET_TABLE,
    sql,
)
from ires_fetch.storage import create_parquet_view, output_directory, write_parquet


def _response(
    request_type: str, product_ids: tuple[str, ...], path_id: int | None = None
) -> IresResponse:
    body = {
        "MESSAGE": "success",
        "RESULTCOUNT": len(product_ids),
        "STATUSCODE": 400,
        "RESULT": [
            {"PRODUCTID": product_id, "RECALLEVENTID": "58700"}
            for product_id in product_ids
        ],
    }

    is_recalls = request_type == "RECALLS"

    return IresResponse(
        request_type=request_type,
        url="https://www.accessdata.fda.gov/rest/iresapi/recalls/",
        path_id=path_id,
        signature="1755700000",
        display_columns=["productid", "recalleventid"],
        filters=[{"centercd": ["CBER", "CDER"]}] if is_recalls else None,
        sort="productid" if is_recalls else None,
        sort_order="asc" if is_recalls else None,
        start=1 if is_recalls else None,
        rows=2500 if is_recalls else None,
        content_type="application/json;charset=UTF-8",
        content_length=len(json.dumps(body)),
        sha_256="0" * 64,
        fetched_at=datetime(2026, 8, 20, 12, tzinfo=UTC),
        content=json.dumps(body),
    )


@pytest.fixture()
def connection() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")

    con.execute(sql("responses.ddl"))

    return con


def test_sql_renders_the_relation_names():
    assert f"CREATE OR REPLACE TABLE {RESPONSES_TABLE}" in sql("responses.ddl")

    assert f"FROM {RECALLS_SOURCE_VIEW}" in sql("workset.create_from_recalls")

    assert f"CREATE OR REPLACE TABLE {WORKSET_TABLE}" in sql(
        "workset.create_from_recalls"
    )


def test_sql_raises_for_an_unknown_address():
    with pytest.raises(KeyError, match="responses.nope"):
        sql("responses.nope")


def test_insert_binds_a_response_in_field_order_and_round_trips_parquet(
    connection, tmp_path
):
    response = _response("RECALLS", ("100063", "100129"))

    connection.execute(sql("responses.insert"), tuple(response))

    assert connection.execute(sql("responses.count")).fetchone() == (1,)

    directory = output_directory(str(tmp_path), "raw/api_responses/recalls/run_id=1")

    write_parquet(connection, RESPONSES_TABLE, directory)

    assert create_parquet_view(connection, str(tmp_path), "_written")

    # `fetched_at` is read back as epoch milliseconds: DuckDB needs `pytz` to hand a
    # TIMESTAMPTZ to Python, and this project does not carry it.
    (row,) = connection.execute(
        "SELECT * REPLACE (epoch_ms(fetched_at) AS fetched_at) FROM _written"
    ).fetchall()

    fetched_at_ms = int(response.fetched_at.timestamp() * 1000)

    assert row == tuple(response._replace(fetched_at=fetched_at_ms))

    types = {
        name: type_
        for name, type_, *_ in connection.execute("DESCRIBE _written").fetchall()
    }

    assert types["filters"] == "MAP(VARCHAR, VARCHAR[])[]"

    assert types["display_columns"] == "VARCHAR[]"

    assert types["fetched_at"] == "TIMESTAMP WITH TIME ZONE"


def test_write_parquet_appends_to_a_local_directory(connection, tmp_path):
    directory = output_directory(str(tmp_path), "run_id=1")

    connection.execute(sql("responses.insert"), tuple(_response("RECALLS", ("1",))))

    write_parquet(connection, RESPONSES_TABLE, directory)

    write_parquet(connection, RESPONSES_TABLE, directory)

    create_parquet_view(connection, directory, "_written")

    assert connection.execute("SELECT count(*) FROM _written").fetchone() == (2,)


def test_create_parquet_view_reports_an_empty_directory(connection, tmp_path):
    assert not create_parquet_view(connection, str(tmp_path / "nothing"), "_empty")


def _create_workset(connection, json_path: str) -> None:
    connection.execute(
        f"CREATE OR REPLACE VIEW {RECALLS_SOURCE_VIEW} AS "
        f"SELECT * FROM {RESPONSES_TABLE}"
    )

    connection.execute(sql("workset.create_from_recalls"), (json_path, "RECALLS"))


def test_the_workset_is_the_distinct_ids_of_the_recalls_responses_only(connection):
    for response in (
        _response("RECALLS", ("100129", "100063")),
        _response("RECALLS", ("100063", "100186")),
        _response("PRODUCT", ("999",)),
    ):
        connection.execute(sql("responses.insert"), tuple(response))

    _create_workset(connection, "$.PRODUCTID")

    assert connection.execute(sql("workset.count")).fetchone() == (3,)

    assert connection.execute(sql("workset.all_ids")).fetchall() == [
        (100063,),
        (100129,),
        (100186,),
    ]

    _create_workset(connection, "$.RECALLEVENTID")

    assert connection.execute(sql("workset.all_ids")).fetchall() == [(58700,)]


def test_keep_remaining_drops_only_what_the_partition_already_holds(connection):
    for response in (
        _response("PRODUCT", ("100063",), path_id=100063),
        _response("PRODUCT", ("100129",), path_id=100129),
        _response("CODE_INFO", ("100186",), path_id=100186),
    ):
        connection.execute(sql("responses.insert"), tuple(response))

    connection.execute(
        f"CREATE TABLE {WORKSET_TABLE} AS "
        "SELECT * FROM (VALUES (100063), (100129), (100186)) AS ids(id)"
    )

    connection.execute(
        f"CREATE VIEW {FETCHED_RESPONSES_VIEW} AS SELECT * FROM {RESPONSES_TABLE}"
    )

    connection.execute(sql("workset.keep_remaining"), ("PRODUCT",))

    # 100186 was fetched for another endpoint, so it is still this one's to fetch.
    assert connection.execute(sql("workset.all_ids")).fetchall() == [(100186,)]

    assert connection.execute(sql("workset.count")).fetchone() == (1,)


def test_keep_remaining_keeps_the_whole_workset_against_an_empty_partition(connection):
    connection.execute(
        f"CREATE TABLE {WORKSET_TABLE} AS SELECT * FROM (VALUES (2), (1)) AS ids(id)"
    )

    connection.execute(
        f"CREATE VIEW {FETCHED_RESPONSES_VIEW} AS SELECT * FROM {RESPONSES_TABLE}"
    )

    connection.execute(sql("workset.keep_remaining"), ("PRODUCT",))

    assert connection.execute(sql("workset.all_ids")).fetchall() == [(1,), (2,)]
