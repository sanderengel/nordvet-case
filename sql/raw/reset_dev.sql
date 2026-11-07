-- TEST ONLY: drops the raw and staging tables. The database tests run it against nordvet_test
-- to start from empty. Not part of the pipeline.

DROP TABLE IF EXISTS staging.appointment;
DROP TABLE IF EXISTS staging.clinic;
DROP TABLE IF EXISTS raw.page;
DROP TABLE IF EXISTS raw.run;
GO
