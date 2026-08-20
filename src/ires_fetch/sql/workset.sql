-- The ids a by-id endpoint is fetched for: where they come from, and what is left of them.

-- The workset is cut from a prior `RECALLS` run's responses, registered over parquet as
-- `recalls_source`. `POST /recalls/` answers `RESULT` as a list of records keyed by
-- upper-cased column name, so the column is addressed as a bound JSON path, e.g.
-- `$.PRODUCTID`. It stays a table rather than a Python list: a workset of tens of
-- thousands of ids is cheap to join here and expensive to bind back in.


-- name: create_from_recalls
CREATE OR REPLACE TABLE {{ workset }} AS
SELECT DISTINCT CAST(json_extract_string(record, $1) AS BIGINT) AS id
FROM (
    SELECT unnest(json_extract(content, '$.RESULT[*]')) AS record
    FROM {{ recalls_source }}
    WHERE request_type = $2
)
WHERE json_extract_string(record, $1) IS NOT NULL;


-- name: count
SELECT count(*) FROM {{ workset }};


-- name: all_ids
SELECT id
FROM {{ workset }}
ORDER BY id;


-- name: keep_remaining
--
-- Narrow the workset to what the run's target partition, registered over parquet as
-- `fetched_responses`, does not already hold. An id counts as fetched only once a
-- response for it was written, so a request that failed -- nothing is staged for those --
-- stays in the workset. Replacing the table it reads leaves `count` and `all_ids` to
-- speak for the resumed run as they do for a fresh one.
CREATE OR REPLACE TABLE {{ workset }} AS
SELECT workset.id
FROM {{ workset }} AS workset
ANTI JOIN {{ fetched_responses }} AS fetched
    ON
        workset.id = fetched.path_id
        AND fetched.request_type = $1;
