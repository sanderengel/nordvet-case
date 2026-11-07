-- Rebuild staging.appointment from raw (D-027): one typed row per id, the latest version.
-- Latest = highest updated_at; ties go to the page stored later. The four stamps are fixed-width
-- ISO strings, so comparing them as text is the same as comparing them as times.
-- scheduled_at arrives without seconds ('2026-10-01T08:00+02:00'); ':00' is added so the
-- conversion never depends on how lenient the server is about a missing seconds part.
-- Run inside a transaction: TRUNCATE is transactional in SQL Server.

TRUNCATE TABLE staging.appointment;
GO

WITH src AS (
    SELECT p.page_id, j.*
    FROM raw.page AS p
    CROSS APPLY OPENJSON(p.body, N'$.data') WITH (
        id               VARCHAR(50) N'$.id',
        clinic_id        VARCHAR(50) N'$.clinic_id',
        vet_id           VARCHAR(50) N'$.vet_id',
        service          VARCHAR(50) N'$.service',
        scheduled_at     VARCHAR(50) N'$.scheduled_at',
        duration_minutes INT         N'$.duration_minutes',
        status           VARCHAR(50) N'$.status',
        created_at       VARCHAR(50) N'$.created_at',
        updated_at       VARCHAR(50) N'$.updated_at',
        cancelled_at     VARCHAR(50) N'$.cancelled_at',
        deleted_at       VARCHAR(50) N'$.deleted_at'
    ) AS j
    WHERE p.endpoint = 'appointments'
),
ranked AS (
    SELECT src.*,
           ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated_at DESC, page_id DESC) AS rn
    FROM src
)
INSERT INTO staging.appointment
    (id, clinic_id, vet_id, service, scheduled_at, duration_minutes, status,
     created_at, updated_at, cancelled_at, deleted_at)
SELECT id, clinic_id, vet_id, service,
       CAST(CASE WHEN LEN(scheduled_at) = 22 THEN STUFF(scheduled_at, 17, 0, N':00') ELSE scheduled_at END
            AS DATETIMEOFFSET(0)),
       duration_minutes, status,
       CAST(CAST(created_at AS DATETIMEOFFSET(0)) AS DATETIME2(0)),
       CAST(CAST(updated_at AS DATETIMEOFFSET(0)) AS DATETIME2(0)),
       CAST(CAST(cancelled_at AS DATETIMEOFFSET(0)) AS DATETIME2(0)),
       CAST(CAST(deleted_at AS DATETIMEOFFSET(0)) AS DATETIME2(0))
FROM ranked
WHERE rn = 1;
GO
