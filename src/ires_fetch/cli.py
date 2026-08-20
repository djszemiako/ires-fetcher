import logging
from collections.abc import Generator, Iterable
from itertools import batched
from typing import Annotated, NamedTuple, Optional

import duckdb
import typer

from ires_fetch import http
from ires_fetch.api import (
    FetchOutcome,
    IresCredentials,
    IresRequest,
    count_recalls,
    fetch_all,
    plan_id_requests,
    plan_recall_pages,
    plan_single_request,
    rows_per_page,
)
from ires_fetch.constants import (
    API_REQUEST_HEADERS,
    DEFAULT_BATCH_SIZE,
    DEFAULT_FILTERS,
    DOCS_REQUEST_HEADERS,
    IresRequestTypes,
)
from ires_fetch.docs import EndpointDocs, fetch_spec, parse_endpoint_docs
from ires_fetch.statements import (
    FETCHED_RESPONSES_VIEW,
    RECALLS_SOURCE_VIEW,
    RESPONSES_TABLE,
    sql,
)
from ires_fetch.storage import (
    connect,
    create_parquet_view,
    is_remote,
    output_directory,
    write_parquet,
)

logger = logging.getLogger("ires_fetch")

app = typer.Typer(add_completion=False)


class IncompleteFetchError(Exception):
    """
    Some requests failed. Every success was written before this is raised.
    """


class StagedBatch(NamedTuple):
    staged: int
    failed: tuple[IresRequest, ...]


def _document_endpoint(request_type: IresRequestTypes) -> EndpointDocs:
    with http.new_client(DOCS_REQUEST_HEADERS) as client:
        spec = fetch_spec(client)

    docs = parse_endpoint_docs(spec, request_type)

    logger.info(
        "Documented %s: display_columns=%s sort_columns=%s filter_columns=%s",
        request_type.name,
        docs.display_columns,
        docs.sort_columns,
        docs.filter_columns,
    )

    return docs


def _count_recalls(credentials: IresCredentials) -> int:
    with http.new_client(API_REQUEST_HEADERS) as client:
        total = count_recalls(client, credentials, list(DEFAULT_FILTERS))

    logger.info("Counted %s recalls for filters %s", total, DEFAULT_FILTERS)

    return total


def _stream_ids(
    connection: duckdb.DuckDBPyConnection, address: str, batch_size: int
) -> Generator[int]:
    """
    Pull the ids of a statement out of DuckDB `batch_size` rows at a time, so the producer
    hands over one batch of work at a time instead of the whole workset at once.

    The stream runs on its own cursor. On the run's own connection the staging writes
    between batches replace the open result set, and the next pull reads THEIR row count
    instead of the ids -- silently, no error.

    Args:
        connection: Active connection
        address: A catalog statement selecting one column of ids
        batch_size: Rows per pull, which is the run's batch size

    Returns:
        Generator of ids, in the statement's order
    """
    cursor = connection.cursor()

    try:
        ids = cursor.sql(sql(address))

        while rows := ids.fetchmany(batch_size):
            for path_id, *_ in rows:
                yield path_id
    finally:
        cursor.close()


def _create_workset(
    connection: duckdb.DuckDBPyConnection,
    request_type: IresRequestTypes,
    ids_source: str,
) -> int:
    """
    Cut the run's workset -- the distinct ids of the endpoint's id column -- out of a
    prior `RECALLS` run's parquet.

    Args:
        connection: Active connection
        request_type: A by-id endpoint
        ids_source: Parquet directory of the `RECALLS` run the ids come from

    Returns:
        How many ids the endpoint is fetched for before anything is resumed
    """
    if not create_parquet_view(connection, ids_source, RECALLS_SOURCE_VIEW):
        raise FileNotFoundError(f"No RECALLS responses found at {ids_source}")

    id_column = request_type.id_column or ""

    connection.execute(
        sql("workset.create_from_recalls"),
        (f"$.{id_column.upper()}", IresRequestTypes.RECALLS.name),
    )

    (planned,) = connection.execute(sql("workset.count")).fetchone() or (0,)

    logger.info("Selected %s distinct %s values", planned, id_column)

    return planned


def _workset_ids(
    connection: duckdb.DuckDBPyConnection,
    request_type: IresRequestTypes,
    ids_source: str,
    directory: str,
    force: bool,
    batch_size: int,
) -> Generator[int]:
    """
    The ids the run fetches: its workset, less every id the target partition already
    holds. An id counts as fetched once a response for it was written, so a request that
    failed -- or one an interrupted run never reached -- is still part of the workset.

    Args:
        connection: Active connection
        request_type: A by-id endpoint
        ids_source: Parquet directory of the `RECALLS` run the ids come from
        directory: The run's target partition
        force: Whether to fetch the workset whole, resuming nothing
        batch_size: How many ids to pull from DuckDB at a time

    Returns:
        Generator of the ids to fetch, in id order, one batch at a time
    """
    planned = _create_workset(connection, request_type, ids_source)

    if force:
        logger.info("--force: fetching all %s ids again", planned)

    elif not create_parquet_view(connection, directory, FETCHED_RESPONSES_VIEW):
        logger.info("Nothing at %s yet; the whole workset stands", directory)

    else:
        connection.execute(sql("workset.keep_remaining"), (request_type.name,))

        (remaining,) = connection.execute(sql("workset.count")).fetchone() or (0,)

        logger.info(
            "Resuming from %s: %s of %s %s values left to fetch",
            directory,
            remaining,
            planned,
            request_type.id_column,
        )

    return _stream_ids(connection, "workset.all_ids", batch_size)


def _warn_unresumable(request_type: IresRequestTypes, force: bool) -> None:
    """
    Note that an endpoint keyed by neither `id_column` nor anything else is refetched
    whole. `POST /recalls/` is paged by offset and the parameterless endpoints are one
    call, so neither has a key a partition could be resumed on.
    """
    if force:
        return

    logger.info(
        "%s has no id column to resume on; every planned request is fetched, as under "
        "--force",
        request_type.name,
    )


def _plan_requests(
    connection: duckdb.DuckDBPyConnection,
    credentials: IresCredentials,
    docs: EndpointDocs,
    rows: Optional[int],
    ids_source: Optional[str],
    directory: str,
    force: bool,
    batch_size: int,
) -> Generator[IresRequest]:
    request_type = docs.request_type

    display_columns = list(docs.display_columns)

    if request_type.accepts_payload:
        _warn_unresumable(request_type, force)

        total = _count_recalls(credentials)

        page_rows = rows or rows_per_page(display_columns)

        logger.info("Planned %s pages of %s rows", -(-total // page_rows), page_rows)

        yield from plan_recall_pages(
            total, display_columns, page_rows, list(DEFAULT_FILTERS)
        )

        return

    if request_type.needs_ids:
        if ids_source is None:
            raise typer.BadParameter(
                f"--ids-source is required for {request_type.name}: it names the "
                "parquet directory of a prior RECALLS run"
            )

        ids = _workset_ids(
            connection, request_type, ids_source, directory, force, batch_size
        )

        yield from plan_id_requests(request_type, display_columns, ids)

        return

    _warn_unresumable(request_type, force)

    yield from plan_single_request(request_type, display_columns)


def _limit(
    requests: Iterable[IresRequest], limit: Optional[int]
) -> Generator[IresRequest]:
    for index, request in enumerate(requests):
        if limit is not None and index >= limit:
            logger.warning("Request limit %s reached; the rest are dropped", limit)

            return

        yield request


def _stage_responses(
    connection: duckdb.DuckDBPyConnection, outcomes: Iterable[FetchOutcome]
) -> StagedBatch:
    """
    Insert each response of one batch into the staging table as it completes.

    The table is recreated per batch, so what the batch appends to the target partition is
    that batch alone.

    Args:
        connection: Active connection
        outcomes: What the batch's requests came back as

    Returns:
        How many responses were staged, and the requests that failed
    """
    connection.execute(sql("responses.ddl"))

    insert = sql("responses.insert")

    def _stage() -> Generator[IresRequest]:
        for outcome in outcomes:
            if outcome.response is None:
                yield outcome.request

                continue

            connection.execute(insert, tuple(outcome.response))

    failed = tuple(_stage())

    (staged,) = connection.execute(sql("responses.count")).fetchone() or (0,)

    return StagedBatch(staged, failed)


@app.command()
def fetch(
    run_id: Annotated[str, typer.Option(help="Run identifier, e.g. a date")],
    request_type: Annotated[
        IresRequestTypes,
        typer.Option(
            help="Endpoint to fetch; one endpoint per run", case_sensitive=True
        ),
    ],
    dest: Annotated[
        str, typer.Option(help="Destination root: a local directory or a gs:// URI")
    ],
    authorization_user: Annotated[
        str, typer.Option(envvar="IRES_AUTHORIZATION_USER", help="iRES API user")
    ],
    authorization_key: Annotated[
        str, typer.Option(envvar="IRES_AUTHORIZATION_KEY", help="iRES API key")
    ],
    ids_source: Annotated[
        Optional[str],
        typer.Option(
            help="Parquet directory of a prior RECALLS run; required by the by-id endpoints"
        ),
    ] = None,
    rows: Annotated[
        Optional[int],
        typer.Option(help="Rows per RECALLS page; defaults to the documented cap"),
    ] = None,
    batch_size: Annotated[
        int,
        typer.Option(
            min=1,
            help="Requests per batch; each batch is written before the next is fetched",
        ),
    ] = DEFAULT_BATCH_SIZE,
    force: Annotated[
        bool,
        typer.Option(
            help="Fetch the whole workset again, ids the target partition already holds "
            "included; their rows are appended a second time"
        ),
    ] = False,
    max_workers: Annotated[int, typer.Option(help="Concurrent requests")] = 4,
    limit: Annotated[
        Optional[int],
        typer.Option(help="Fetch at most this many requests; smoke tests"),
    ] = None,
    logging_level: Annotated[str, typer.Option(help="Logging level")] = "INFO",
) -> None:
    """
    Fetch one iRES API endpoint and keep every response verbatim.

    Reads the endpoint's documented columns from the API documentation, plans the
    requests that cover it (offset pages for `POST /recalls/`, one request per id
    otherwise), drops the ids the target partition already holds unless `--force` says
    otherwise, then fetches the rest in batches through a worker pool with a politeness
    window between calls. Each batch is staged in DuckDB and appended to the partition
    as parquet, so an interrupted run keeps what its earlier batches wrote and the next
    run resumes from there.
    """
    logging.basicConfig(
        level=logging_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    logger.info(
        "run_id=%s request_type=%s dest=%s ids_source=%s rows=%s batch_size=%s "
        "force=%s max_workers=%s limit=%s",
        run_id,
        request_type.name,
        dest,
        ids_source,
        rows,
        batch_size,
        force,
        max_workers,
        limit,
    )

    credentials = IresCredentials(authorization_user, authorization_key)

    directory = output_directory(
        dest, f"raw/api_responses/{request_type.name.lower()}/run_id={run_id}"
    )

    connection = connect(
        needs_remote=is_remote(dest) or bool(ids_source and is_remote(ids_source))
    )

    docs = _document_endpoint(request_type)

    requests = _limit(
        _plan_requests(
            connection,
            credentials,
            docs,
            rows,
            ids_source,
            directory,
            force,
            batch_size,
        ),
        limit,
    )

    failed: list[IresRequest] = []

    written = 0

    batches = 0

    # A short final batch is the normal case, not an error.
    for batch in batched(requests, batch_size, strict=False):
        batches += 1

        logger.info("Batch %s: fetching %s requests", batches, len(batch))

        outcome = _stage_responses(
            connection, fetch_all(batch, credentials, max_workers)
        )

        failed.extend(outcome.failed)

        if outcome.staged:
            write_parquet(connection, RESPONSES_TABLE, directory)

        written += outcome.staged

        logger.info(
            "Batch %s: appended %s responses, %s failed",
            batches,
            outcome.staged,
            len(outcome.failed),
        )

    if not batches:
        logger.info("Nothing left to fetch for %s at %s", request_type.name, directory)

        return

    logger.info(
        "Wrote %s responses in %s batches to %s, %s failed",
        written,
        batches,
        directory,
        len(failed),
    )

    if failed:
        raise IncompleteFetchError(
            f"{len(failed)} of the {request_type.name} requests failed; "
            f"first: {failed[0].url} start={failed[0].start}"
        )


if __name__ == "__main__":
    app()
