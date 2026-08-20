"""
Relation names and DuckDB SQL of the fetch.

Statements live in `sql/`, one file per relation, each introduced by an aiosql-style
`-- name:` line and addressed as `sql("<file stem>.<name>")`. The constants below are the
interpolation allowlist: `IDENTIFIERS` is built from them and is the only thing a template
can see. Interpolation is STRUCTURE only; run-time values bind as `$n` parameters.
"""

from functools import cache

from ires_fetch.catalog import render

_SQL_PACKAGE = "ires_fetch.sql"

# Every response of a run, one row per call. See `sql/responses.sql`.
RESPONSES_TABLE = "_responses"

# Lazy view over a prior `RECALLS` run's parquet, from which the by-id endpoints draw
# their ids. Registered straight over parquet; see `sql/workset.sql`.
RECALLS_SOURCE_VIEW = "_recalls_source"

# The ids of the run, cut out of that view.
WORKSET_TABLE = "_workset"

# Lazy view over what the run's own target partition already holds, which a resumed run
# subtracts from its workset. Registered straight over parquet; see `sql/workset.sql`.
FETCHED_RESPONSES_VIEW = "_fetched_responses"

IDENTIFIERS = {
    "responses": RESPONSES_TABLE,
    "recalls_source": RECALLS_SOURCE_VIEW,
    "workset": WORKSET_TABLE,
    "fetched_responses": FETCHED_RESPONSES_VIEW,
}


@cache
def sql(address: str) -> str:
    """
    Render one catalog statement, e.g. `sql("responses.insert")`.

    Args:
        address: `<file stem>.<statement name>` within `ires_fetch/sql/`

    Returns:
        Executable SQL, with run-time values still to be bound as `$n` parameters
    """
    return render(_SQL_PACKAGE, address, IDENTIFIERS)
