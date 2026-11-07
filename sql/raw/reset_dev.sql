-- DEV ONLY: drops the raw and staging tables so a load can be timed from empty.
-- Not part of the pipeline; run only through `--reset` of a dev tool.

DROP TABLE IF EXISTS staging.appointment;
DROP TABLE IF EXISTS staging.clinic;
DROP TABLE IF EXISTS raw.page;
DROP TABLE IF EXISTS raw.run;
GO
