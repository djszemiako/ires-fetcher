"""
DuckDB as the serializer: a connection, a parquet writer and a parquet reader.

Destinations are a local directory or a `gs://` / `s3://` URI. Remote access goes through
DuckDB's `httpfs`; GCS needs HMAC credentials in `GCS_HMAC_KEY_ID` / `GCS_HMAC_SECRET_KEY`.
"""

import logging
import os
import re
from pathlib import Path
from typing import Optional

import duckdb

logger = logging.getLogger(__name__)

REMOTE_SCHEMES: tuple[str, ...] = ("gs", "s3", "s3a")

# NOTE [string interpolation]: DuckDB takes neither identifiers nor `COPY` paths as
# parameters, so relation names and storage paths are interpolated below. Relation names
# come from `statements.py` and are validated; paths have their quotes escaped.
_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

DuckDbConfig = dict[str, str | int | bool]


def validate_identifier(name: str) -> str:
    if not _IDENTIFIER_PATTERN.match(name):
        raise ValueError(f"invalid relation name: {name!r}")

    return name


def _quote_path(path: str) -> str:
    return "'" + path.replace("'", "''") + "'"


def scheme_of(uri: str) -> str:
    """
    The URI scheme, or `file` for a bare path.
    """
    scheme, separator, _ = uri.partition("://")

    return scheme if separator else "file"


def is_remote(uri: str) -> bool:
    return scheme_of(uri) in REMOTE_SCHEMES


def connect(
    needs_remote: bool, config: Optional[DuckDbConfig] = None
) -> duckdb.DuckDBPyConnection:
    """
    An in-memory connection, with `httpfs` and cloud credentials when a run reads or
    writes remote storage.

    Args:
        needs_remote: Whether any path of the run is a cloud URI
        config: DuckDB settings, e.g. `{"threads": 4, "memory_limit": "4GB"}`

    Returns:
        The configured connection
    """
    connection = duckdb.connect(database=":memory:", config=config or {})

    if not needs_remote:
        return connection

    connection.execute("INSTALL httpfs; LOAD httpfs;")

    key_id = os.getenv("GCS_HMAC_KEY_ID")

    secret = os.getenv("GCS_HMAC_SECRET_KEY")

    if key_id and secret:
        connection.execute(
            "CREATE SECRET (TYPE gcs, KEY_ID ?, SECRET ?);", (key_id, secret)
        )

    return connection


def output_directory(dest: str, key: str) -> str:
    """
    The directory a run's parquet is written to, spelled the way DuckDB's `COPY` takes it:
    a cloud URI, or a bare path for the local filesystem, which DuckDB has no scheme for.

    Args:
        dest: Destination root: a local directory or a cloud URI
        key: Path below the root

    Returns:
        The directory, created up to its parent when local
    """
    if is_remote(dest):
        return f"{dest.rstrip('/')}/{key.strip('/')}"

    local_path = Path(dest.removeprefix("file://")) / key.strip("/")

    # `COPY` creates the leaf directory only.
    local_path.parent.mkdir(parents=True, exist_ok=True)

    return str(local_path)


def write_parquet(
    connection: duckdb.DuckDBPyConnection, source: str, directory: str
) -> None:
    """
    Append a relation to a parquet directory via `COPY TO`, keeping what was there.

    A run writes one batch at a time and a resumed run adds to what earlier runs left, so
    every write appends: `{uuid}` in the filename pattern is what keeps the files of one
    directory from colliding. Nothing here ever clears a directory -- refetching an id
    duplicates its rows, and starting a partition over means deleting it first.

    Args:
        connection: Active connection
        source: Name of a table or view
        directory: Destination directory, from `output_directory`
    """
    options = (
        "FORMAT PARQUET",
        "COMPRESSION 'ZSTD'",
        "PER_THREAD_OUTPUT true",
        "FILENAME_PATTERN 'part-{i}-{uuid}'",
        # `APPEND` is a local-filesystem check that the directory may be added to; DuckDB
        # cannot make it remotely, where writing into the directory is all there is.
        "OVERWRITE_OR_IGNORE true" if is_remote(directory) else "APPEND true",
    )

    logger.info("Writing parquet to %s", directory)

    # See NOTE [string interpolation].
    connection.execute(
        f"COPY {validate_identifier(source)} TO {_quote_path(directory)} "
        f"({', '.join(options)})"
    )


def create_parquet_view(
    connection: duckdb.DuckDBPyConnection, directory: str, name: str
) -> bool:
    """
    Register a lazy view over every parquet file below a directory.

    Files are unioned by name, so a directory whose earlier files predate a column of the
    current schema still reads, that column NULL for them.

    Args:
        connection: Active connection
        directory: Directory holding `.parquet` files, possibly in subdirectories
        name: Name of the view

    Returns:
        Whether any file matched
    """
    glob = f"{directory.removeprefix('file://').rstrip('/')}/**/*.parquet"

    try:
        # See NOTE [string interpolation].
        connection.execute(
            f"CREATE OR REPLACE VIEW {validate_identifier(name)} AS "
            f"SELECT * FROM read_parquet({_quote_path(glob)}, "
            "hive_partitioning = false, union_by_name = true)"
        )
    except duckdb.IOException as error:
        if "No files found" in str(error):
            return False

        raise

    return True
