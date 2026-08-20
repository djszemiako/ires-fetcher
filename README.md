# ires-fetch

Fetch the FDA iRES (Enforcement Reports) API in full and keep every response verbatim as parquet.

One endpoint per run. `POST /recalls/` is paged by record offset after a one-row probe reports the filtered total; the by-id `GET` endpoints take one request per id drawn from a prior `RECALLS` run. Every response is stored as the raw JSON text it arrived as, alongside what was asked and when.

Requests are fetched in batches and every batch is appended to the run's parquet as it finishes, so an interrupted run keeps what it had fetched. A by-id run reads its own target partition first and drops the ids already there, which is what makes a re-run a resume rather than a repeat.

## Install

Python 3.14. Either locally,

```sh
uv sync --group dev
```

or entirely in Docker (see [Docker](#docker) below), which is how the pipeline is meant to run.

## Run

```sh
export IRES_AUTHORIZATION_USER=...
export IRES_AUTHORIZATION_KEY=...

# The filtered recalls dataset (CBER + CDER), every documented column, 2500 rows a page.
uv run ires-fetch --run-id 20260820 --request-type RECALLS --dest ./out

# A by-id endpoint, fed the distinct product ids of that RECALLS run.
uv run ires-fetch --run-id 20260820 --request-type PRODUCT --dest ./out \
    --ids-source ./out/raw/api_responses/recalls/run_id=20260820
```

Output lands at `<dest>/raw/api_responses/<endpoint>/run_id=<run-id>/*.parquet`. `--dest` may be a local directory or a `gs://` URI (DuckDB `httpfs`; set `GCS_HMAC_KEY_ID` / `GCS_HMAC_SECRET_KEY`). `--limit N` caps the requests for a smoke test; `--max-workers` bounds concurrency (each worker has its own client and sleeps through a politeness window before every call).

### Batching and resuming

Repeat the second command above and it fetches only what the first one missed:

```sh
uv run ires-fetch --run-id 20260820 --request-type PRODUCT --dest ./out \
    --ids-source ./out/raw/api_responses/recalls/run_id=20260820 --batch-size 500
```

- `--batch-size N` (default 1000) is how many requests are fetched, staged and appended before the next batch starts. Smaller batches lose less to an interruption and cost one parquet write each.
- The producer is batched too: a by-id run pulls its ids out of DuckDB `batch_size` rows at a time (`fetchmany`) rather than materialising the whole workset in Python, so the chain from the workset table to the parquet write is lazy end to end.
- The workset is anti-joined against the `path_id`s the target partition already holds, so a resumed run asks only for what is missing. An id counts as fetched once its response was written: requests that failed come back on the next run.
- `--force` skips that check and fetches the whole workset again. Writes always append, so the refetched ids are written a second time rather than replacing the first; starting a partition over means deleting its directory.
- `RECALLS` (paged by offset) and `PRODUCT_TYPES` (one call) have no id column to resume on. They log that and fetch in full, batching all the same.

## Output schema

| column | type | |
|---|---|---|
| `request_type` | VARCHAR | our endpoint name, e.g. `RECALLS` |
| `url` | VARCHAR | full URL, without the signature |
| `path_id` | BIGINT | the id the url was built from, NULL where the endpoint takes none |
| `signature` | VARCHAR | the cache-busting query value we minted |
| `display_columns` | VARCHAR[] | `displaycolumns` sent, or the columns a GET documents |
| `filters` | MAP(VARCHAR, VARCHAR[])[] | `[{'centercd': ['CBER', 'CDER']}]`, NULL where not accepted |
| `sort`, `sort_order` | VARCHAR | `productid` / `asc`, NULL where not accepted |
| `start`, `rows` | INTEGER | page offset and size, NULL where not accepted |
| `content_type`, `content_length` | VARCHAR, BIGINT | response headers (decoded length when the header is absent) |
| `sha_256` | VARCHAR | of the response body |
| `fetched_at` | TIMESTAMPTZ | UTC |
| `content` | VARCHAR | the response body, verbatim |

## Layout

- `constants.py` - endpoints, headers, filters, page caps
- `docs.py` - reads the documented columns out of the Swagger spec the docs page loads
- `http.py` - httpx client per worker, backoff and retries
- `api.py` - request planning, fetching, the worker pool
- `sql/` + `statements.py` + `catalog.py` - DuckDB statements as Jinja-templated `.sql` files
- `storage.py` - DuckDB connection, parquet write and read
- `cli.py` - the typer command

## Docker

`Dockerfile` builds a `python:3.14-slim` image with the locked dependencies (`runtime` target) or the dev group on top (`dev` target); `Makefile` wraps `docker buildx` and `docker run` so nothing needs a local Python. `make help` lists the targets.

```sh
export IRES_AUTHORIZATION_USER=...
export IRES_AUTHORIZATION_KEY=...

make build                        # docker buildx build --target runtime
make test lint                    # pytest, ruff and sqlfluff in the dev image

make recalls RUN_ID=20260820      # one stage; writes to ./out/raw/api_responses/recalls/run_id=20260820
make product RUN_ID=20260820      # a by-id stage, fed that run's RECALLS parquet
make pipeline RUN_ID=20260820     # every stage, sequentially, under one run id
make pipeline RUN_ID=20260820     # again: the by-id stages resume where they stopped
```

There is one target per endpoint, in pipeline order: `recalls`, `product-types`, `event`, `product`, `event-products`, `code-info`, `product-history`, `event-product-history`, `press-release-urls`. Variables: `RUN_ID` (default: today, UTC), `OUT` (what backs `/data` in the container: a host directory, default `./out`, or a Docker volume name), `DEST` (default `/data`; a `gs://` URI also works, with `GCS_HMAC_KEY_ID` / `GCS_HMAC_SECRET_KEY` exported), `MAX_WORKERS`, `LIMIT`, `ROWS`, `BATCH_SIZE`, `FORCE` (`FORCE=1` to refetch what a partition already holds), `LOGGING_LEVEL`. The image runs as the invoking user's uid/gid so the parquet in `OUT` is theirs. `make lock` regenerates `uv.lock` in Docker.

## Develop

```sh
uv run pytest
uv run ruff check . && uv run ruff format .
uv run sqlfluff lint src/ires_fetch/sql
```
