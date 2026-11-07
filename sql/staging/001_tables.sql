-- Staging layer (D-027): typed, one row per id, the latest version seen in raw.
-- Only the fields the model needs (D-023). patient_ref stays in raw (D-027).
-- No CHECK constraints on status or service (D-023).

IF SCHEMA_ID(N'staging') IS NULL EXEC (N'CREATE SCHEMA staging');
GO

IF OBJECT_ID(N'staging.appointment', N'U') IS NULL
CREATE TABLE staging.appointment (
    id               VARCHAR(16)        NOT NULL CONSTRAINT pk_staging_appointment PRIMARY KEY,
    clinic_id        VARCHAR(6)         NOT NULL,
    vet_id           VARCHAR(5)         NOT NULL,
    service          VARCHAR(12)        NOT NULL,
    scheduled_at     DATETIMEOFFSET(0)  NOT NULL,  -- local wall time with its offset, as the API gives it
    duration_minutes INT                NOT NULL,
    status           VARCHAR(9)         NOT NULL,
    created_at       DATETIME2(0)       NOT NULL,  -- UTC
    updated_at       DATETIME2(0)       NOT NULL,  -- UTC
    cancelled_at     DATETIME2(0)       NULL,      -- UTC, only on cancelled rows
    deleted_at       DATETIME2(0)       NULL       -- UTC, only on soft-deleted rows
);
GO

IF OBJECT_ID(N'staging.clinic', N'U') IS NULL
CREATE TABLE staging.clinic (
    id        VARCHAR(6)    NOT NULL CONSTRAINT pk_staging_clinic PRIMARY KEY,
    name      NVARCHAR(100) NOT NULL,
    city      NVARCHAR(100) NOT NULL,
    country   CHAR(2)       NOT NULL,
    timezone  VARCHAR(40)   NOT NULL,
    active    BIT           NOT NULL,
    opened_on DATE          NOT NULL
);
GO
