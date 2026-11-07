-- Raw layer (D-027): API responses exactly as received, append-only.
-- Nothing in raw is parsed, merged, updated or deleted. Re-runnable: creates only what is missing.

IF SCHEMA_ID(N'raw') IS NULL EXEC (N'CREATE SCHEMA raw');
GO

-- One row per successful run. A failed run writes nothing (D-024), so there is no outcome column.
-- The watermark for the next run is derived from here: the latest started_at minus 10 minutes.
IF OBJECT_ID(N'raw.run', N'U') IS NULL
CREATE TABLE raw.run (
    run_id           INT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_raw_run PRIMARY KEY,
    started_at       DATETIME2(0)  NOT NULL,  -- server's Date header on the run's first response, UTC (D-024)
    finished_at      DATETIME2(0)  NOT NULL,  -- when the last response was received, UTC
    loaded_at        DATETIME2(0)  NOT NULL CONSTRAINT df_raw_run_loaded DEFAULT SYSUTCDATETIME(),
    pages            INT           NOT NULL,
    appointment_rows INT           NOT NULL,  -- rows received, repeats included
    source           NVARCHAR(200) NOT NULL   -- 'api', or the saved pull the pages were replayed from
);
GO

-- One row per API response: the JSON body is kept as text, as it came.
IF OBJECT_ID(N'raw.page', N'U') IS NULL
CREATE TABLE raw.page (
    page_id     BIGINT IDENTITY(1, 1) NOT NULL CONSTRAINT pk_raw_page PRIMARY KEY,
    run_id      INT           NOT NULL CONSTRAINT fk_raw_page_run REFERENCES raw.run (run_id),
    endpoint    VARCHAR(20)   NOT NULL,  -- 'appointments' or 'clinics'
    params      NVARCHAR(400) NOT NULL,  -- request parameters as JSON, never the cursor
    page_no     INT           NOT NULL,  -- 1 = first page of that request sequence
    server_date DATETIME2(0)  NOT NULL,  -- the response's Date header, UTC
    fetched_at  DATETIME2(0)  NOT NULL,  -- when we received it, UTC
    body        NVARCHAR(MAX) NOT NULL   -- the JSON text as received
);
GO

IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = N'ix_raw_page_run' AND object_id = OBJECT_ID(N'raw.page'))
CREATE INDEX ix_raw_page_run ON raw.page (run_id);
GO
