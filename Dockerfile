# syntax=docker/dockerfile:1

ARG PYTHON_VERSION=3.14.7
ARG UV_VERSION=0.12

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

# --- python: the interpreter plus uv, nothing installed -------------------------------
FROM python:${PYTHON_VERSION}-slim-trixie AS python

COPY --from=uv /uv /uvx /usr/local/bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_PYTHON_DOWNLOADS=never \
    PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1

WORKDIR /app

# --- lock / lockfile: `make lock` exports /uv.lock from here --------------------------
FROM python AS lock

COPY pyproject.toml README.md ./
COPY uv.loc[k] ./

RUN --mount=type=cache,target=/root/.cache/uv uv lock

FROM scratch AS lockfile

COPY --from=lock /app/uv.lock /uv.lock

# --- base: the locked runtime dependencies, before the sources ------------------------
# A source edit does not invalidate this layer.
FROM python AS base

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --no-dev --no-install-project

# --- runtime: the package on top of the dependencies ---------------------------------
FROM base AS runtime

ARG UID=1000
ARG GID=1000

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

# Run as the invoking host user so the parquet written to the /data mount is theirs.
RUN (getent group "${GID}" >/dev/null || groupadd --gid "${GID}" ires) \
    && useradd --create-home --uid "${UID}" --gid "${GID}" ires \
    && mkdir -p /data \
    && chown "${UID}:${GID}" /data

USER ires

# `httpfs` backs the gs:// destinations; installed here so a run needs no extension
# download (DuckDB keeps it under $HOME/.duckdb).
RUN python -c "import duckdb; duckdb.connect().execute('INSTALL httpfs')"

VOLUME ["/data"]

ENTRYPOINT ["ires-fetch"]
CMD ["--help"]

# --- dev: the dev group and the tests, for `make test` / `make lint` ------------------
FROM python AS dev

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    --mount=type=bind,source=README.md,target=README.md \
    uv sync --locked --group dev --no-install-project

COPY pyproject.toml uv.lock README.md .sqlfluff ./
COPY src ./src
COPY test ./test

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --group dev

ENTRYPOINT ["uv", "run", "--no-sync"]
CMD ["pytest"]
