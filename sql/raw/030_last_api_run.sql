-- Start of the latest committed API run: the loader's watermark is this time minus
-- the margin. A run record exists only if its pages were committed with it, so every row
-- here is a successful run. Runs with another source label are ignored.
-- Parameter 1: the source label of API runs. Returns one row; NULL when there is none.
SELECT MAX(started_at) FROM raw.run WHERE source = ?;
