-- Every response of a run lands here, one row per call, and leaves in one parquet write.

-- The table IS the output schema: the `IresResponse` record, column for column. `content`
-- is the response body verbatim; `filters` keeps the payload's shape (one single-key
-- mapping per filter) rather than its single-quoted spelling. `path_id` is the id a by-id
-- url was built from, and is the key a resumed run skips on.


-- name: ddl
CREATE OR REPLACE TABLE {{ responses }} (
    request_type VARCHAR NOT NULL,
    url VARCHAR NOT NULL,
    path_id BIGINT,
    signature VARCHAR NOT NULL,
    display_columns VARCHAR[] NOT NULL,
    filters MAP (VARCHAR, VARCHAR[])[],
    sort VARCHAR,
    sort_order VARCHAR,
    start INTEGER,
    rows INTEGER,
    content_type VARCHAR NOT NULL,
    content_length BIGINT NOT NULL,
    sha_256 VARCHAR NOT NULL,
    fetched_at TIMESTAMPTZ NOT NULL,
    content VARCHAR NOT NULL
);


-- name: insert
--
-- Parameter order is `IresResponse` field order, so a record binds as `tuple(response)`.
INSERT INTO {{ responses }} (
    request_type,
    url,
    path_id,
    signature,
    display_columns,
    filters,
    sort,
    sort_order,
    start,
    rows,
    content_type,
    content_length,
    sha_256,
    fetched_at,
    content
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15);


-- name: count
SELECT count(*) FROM {{ responses }};
