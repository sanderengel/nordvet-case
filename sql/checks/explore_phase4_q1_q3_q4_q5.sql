-- Phase 4 exploration (read-only): answers Q1, Q2a, Q3, Q4 and Q5 of phase-4-questions.md.
-- Only SELECTs; nothing is created, changed or deleted. Run from the repo root against nordvet:
--
--   docker exec -i nordvet-sql bash -c '/opt/mssql-tools18/bin/sqlcmd -C -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -d nordvet -W -s "|"' \
--     < sql/checks/explore_phase4_q1_q3_q4_q5.sql > data/phase4_q1_q3_q4_q5.txt 2>&1
--
-- Every section is its own batch, so a section that fails (for example on a time-zone name the server
-- does not know) prints its error and the rest still run. "As of" is the latest run start (D-035).

SET NOCOUNT ON;
GO

PRINT '== 0. Runs in raw.run (D-035: the model is as of the latest start) ==';
SELECT run_id, source, CONVERT(VARCHAR(19), started_at, 126) AS started_at_utc, pages, appointment_rows
FROM raw.run
ORDER BY run_id;
GO

------------------------------------------------------------------------------------------------
PRINT '== Q1a. Local date: CAST(scheduled_at AS DATE) against the raw JSON string (latest version per id) ==';
WITH src AS (
    SELECT p.page_id, j.id, j.scheduled_at, j.updated_at
    FROM raw.page AS p
    CROSS APPLY OPENJSON(p.body, N'$.data') WITH (
        id           VARCHAR(50) N'$.id',
        scheduled_at VARCHAR(50) N'$.scheduled_at',
        updated_at   VARCHAR(50) N'$.updated_at'
    ) AS j
    WHERE p.endpoint = 'appointments'
),
latest AS (
    SELECT id, scheduled_at AS raw_scheduled_at,
           ROW_NUMBER() OVER (PARTITION BY id ORDER BY updated_at DESC, page_id DESC) AS rn
    FROM src
)
SELECT COUNT(*) AS rows_compared,
       MIN(LEN(l.raw_scheduled_at)) AS min_raw_len,
       MAX(LEN(l.raw_scheduled_at)) AS max_raw_len,
       SUM(CASE WHEN CONVERT(CHAR(10), CAST(a.scheduled_at AS DATE), 23) <> LEFT(l.raw_scheduled_at, 10) THEN 1 ELSE 0 END) AS date_mismatches,
       SUM(CASE WHEN CONVERT(CHAR(5), CAST(a.scheduled_at AS TIME), 108) <> SUBSTRING(l.raw_scheduled_at, 12, 5) THEN 1 ELSE 0 END) AS time_mismatches,
       SUM(CASE WHEN DATENAME(TZOFFSET, a.scheduled_at) <> RIGHT(l.raw_scheduled_at, 6) THEN 1 ELSE 0 END) AS offset_mismatches
FROM staging.appointment AS a
JOIN latest AS l ON l.id = a.id AND l.rn = 1;
GO

PRINT '== Q1b. Offsets per clinic time zone, with the local date range of each (DST ended 25 Oct) ==';
SELECT c.timezone,
       DATENAME(TZOFFSET, a.scheduled_at) AS utc_offset,
       COUNT(*) AS n,
       MIN(CAST(a.scheduled_at AS DATE)) AS first_local_date,
       MAX(CAST(a.scheduled_at AS DATE)) AS last_local_date
FROM staging.appointment AS a
JOIN staging.clinic AS c ON c.id = a.clinic_id
GROUP BY c.timezone, DATENAME(TZOFFSET, a.scheduled_at)
ORDER BY c.timezone, first_local_date;

SELECT COUNT(*) AS appointments_without_clinic_row
FROM staging.appointment AS a
WHERE NOT EXISTS (SELECT 1 FROM staging.clinic AS c WHERE c.id = a.clinic_id);
GO

PRINT '== Q1c. Late evenings: does the local date ever differ from the UTC date? ==';
SELECT COUNT(*) AS n_rows,
       SUM(CASE WHEN CAST(scheduled_at AS DATE) <> CAST(SWITCHOFFSET(scheduled_at, '+00:00') AS DATE) THEN 1 ELSE 0 END) AS local_date_differs_from_utc_date,
       SUM(CASE WHEN DATEPART(HOUR, scheduled_at) >= 22 OR DATEPART(HOUR, scheduled_at) < 2 THEN 1 ELSE 0 END) AS starts_22_to_02_local,
       MIN(CAST(scheduled_at AS TIME)) AS earliest_local_start,
       MAX(CAST(scheduled_at AS TIME)) AS latest_local_start
FROM staging.appointment;
GO

------------------------------------------------------------------------------------------------
PRINT '== Q2a. Is a UTC DATETIME2 compared with a DATETIMEOFFSET as UTC? (literal test) ==';
DECLARE @utc   DATETIME2(0)      = '2026-11-06T10:00:00';
DECLARE @local DATETIMEOFFSET(0) = '2026-11-06T11:00:00+01:00';  -- the same instant
SELECT CASE WHEN @utc = @local THEN 'equal (DATETIME2 read as UTC)' ELSE 'NOT equal' END AS implicit_compare,
       CASE WHEN TODATETIMEOFFSET(@utc, '+00:00') = @local THEN 'equal' ELSE 'NOT equal' END AS explicit_utc_compare,
       CAST(@utc AS DATETIMEOFFSET(0)) AS datetime2_cast_to_datetimeoffset;
GO

PRINT '== Q2a. Cancellations at or after the start on the live data, three ways of comparing ==';
SELECT COUNT(*) AS cancelled_not_deleted,
       SUM(CASE WHEN cancelled_at IS NULL THEN 1 ELSE 0 END) AS cancelled_at_missing,
       SUM(CASE WHEN cancelled_at >= scheduled_at THEN 1 ELSE 0 END) AS after_start_implicit,
       SUM(CASE WHEN TODATETIMEOFFSET(cancelled_at, '+00:00') >= scheduled_at THEN 1 ELSE 0 END) AS after_start_explicit_utc,
       SUM(CASE WHEN cancelled_at >= CAST(scheduled_at AS DATETIME2(0)) THEN 1 ELSE 0 END) AS after_start_wrong_local_wall_time
FROM staging.appointment
WHERE status = 'cancelled' AND deleted_at IS NULL;
GO

------------------------------------------------------------------------------------------------
PRINT '== Q3a. Time-zone names stored for the clinics ==';
SELECT timezone, country, COUNT(*) AS clinics, SUM(CAST(active AS INT)) AS active_clinics
FROM staging.clinic
GROUP BY timezone, country
ORDER BY timezone;
GO

PRINT '== Q3b. Does AT TIME ZONE accept the IANA name Europe/Copenhagen? ==';
DECLARE @tz SYSNAME = N'Europe/Copenhagen';
BEGIN TRY
    SELECT CAST('2026-11-06T17:40:32' AS DATETIME2(0)) AT TIME ZONE 'UTC' AT TIME ZONE @tz AS iana_result;
END TRY
BEGIN CATCH
    SELECT ERROR_NUMBER() AS error_number, ERROR_MESSAGE() AS error_message;
END CATCH;
GO

PRINT '== Q3c. Candidate Windows names in sys.time_zone_info ==';
SELECT name, current_utc_offset, is_currently_dst
FROM sys.time_zone_info
WHERE name IN (N'UTC', N'GMT Standard Time') OR name LIKE N'%Europe%' OR name LIKE N'Romance%'
ORDER BY name;
SELECT COUNT(*) AS zones_listed FROM sys.time_zone_info;
GO

PRINT '== Q3d. Latest run start as local time, and conversions across the DST change ==';
DECLARE @run_start DATETIME2(0) = (SELECT MAX(started_at) FROM raw.run);
SELECT @run_start AS run_start_utc,
       @run_start AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time'   AS copenhagen_local,
       @run_start AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS stockholm_local,
       CAST(@run_start AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE) AS as_of_local_date;
SELECT t.utc_instant,
       t.utc_instant AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time'   AS copenhagen,
       t.utc_instant AT TIME ZONE 'UTC' AT TIME ZONE 'W. Europe Standard Time' AS stockholm
FROM (VALUES (CAST('2026-10-24T22:30:00' AS DATETIME2(0))),
             (CAST('2026-10-25T00:30:00' AS DATETIME2(0))),
             (CAST('2026-10-25T23:30:00' AS DATETIME2(0))),
             (CAST('2026-11-06T23:27:03' AS DATETIME2(0)))) AS t (utc_instant);
GO

PRINT '== Q3e. Does every appointment offset agree with its clinic zone (IANA mapped to a Windows name)? ==';
WITH m AS (
    SELECT a.scheduled_at, c.timezone,
           CASE c.timezone WHEN 'Europe/Copenhagen' THEN 'Romance Standard Time'
                           WHEN 'Europe/Stockholm'  THEN 'W. Europe Standard Time' END AS win_zone
    FROM staging.appointment AS a
    JOIN staging.clinic AS c ON c.id = a.clinic_id
)
SELECT timezone, win_zone, COUNT(*) AS n,
       SUM(CASE WHEN win_zone IS NOT NULL
                 AND DATENAME(TZOFFSET, scheduled_at AT TIME ZONE COALESCE(win_zone, 'UTC')) <> DATENAME(TZOFFSET, scheduled_at)
                THEN 1 ELSE 0 END) AS offset_disagrees
FROM m
GROUP BY timezone, win_zone
ORDER BY timezone;
GO

------------------------------------------------------------------------------------------------
PRINT '== Q4a. Past appointments still booked, by days before the as-of date (non-deleted) ==';
DECLARE @run_start DATETIME2(0) = (SELECT MAX(started_at) FROM raw.run);
DECLARE @as_of DATE = CAST(@run_start AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE);
SELECT @run_start AS run_start_utc, @as_of AS as_of_local_date;
SELECT DATEDIFF(DAY, CAST(scheduled_at AS DATE), @as_of) AS days_before_as_of,
       MIN(CAST(scheduled_at AS DATE)) AS scheduled_local_date,
       COUNT(*) AS still_booked
FROM staging.appointment
WHERE status = 'booked' AND deleted_at IS NULL AND CAST(scheduled_at AS DATE) < @as_of
GROUP BY DATEDIFF(DAY, CAST(scheduled_at AS DATE), @as_of)
ORDER BY days_before_as_of;
GO

PRINT '== Q4b. Status mix of the last 10 days before the as-of date, and the as-of day itself (non-deleted) ==';
DECLARE @as_of DATE = CAST((SELECT MAX(started_at) FROM raw.run) AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE);
SELECT CAST(scheduled_at AS DATE) AS local_date,
       DATENAME(WEEKDAY, CAST(scheduled_at AS DATE)) AS weekday,
       SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
       SUM(CASE WHEN status = 'no_show'   THEN 1 ELSE 0 END) AS no_show,
       SUM(CASE WHEN status = 'booked'    THEN 1 ELSE 0 END) AS booked,
       SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
       SUM(CASE WHEN status NOT IN ('completed', 'no_show', 'booked', 'cancelled') THEN 1 ELSE 0 END) AS other_status
FROM staging.appointment
WHERE deleted_at IS NULL
  AND CAST(scheduled_at AS DATE) BETWEEN DATEADD(DAY, -10, @as_of) AND @as_of
GROUP BY CAST(scheduled_at AS DATE)
ORDER BY local_date;
GO

PRINT '== Q4c. Clinics with a still-booked appointment more than 5 days before the as-of date ==';
DECLARE @as_of DATE = CAST((SELECT MAX(started_at) FROM raw.run) AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE);
SELECT a.clinic_id, c.name, c.active, COUNT(*) AS old_booked,
       MIN(CAST(a.scheduled_at AS DATE)) AS oldest_local_date
FROM staging.appointment AS a
LEFT JOIN staging.clinic AS c ON c.id = a.clinic_id
WHERE a.status = 'booked' AND a.deleted_at IS NULL
  AND CAST(a.scheduled_at AS DATE) < DATEADD(DAY, -5, @as_of)
GROUP BY a.clinic_id, c.name, c.active
ORDER BY old_booked DESC;
GO

------------------------------------------------------------------------------------------------
PRINT '== Q5a. Outcomes on appointments that had not ended at the latest run start (non-deleted) ==';
DECLARE @run_start DATETIME2(0) = (SELECT MAX(started_at) FROM raw.run);
DECLARE @rs DATETIMEOFFSET(0) = TODATETIMEOFFSET(@run_start, '+00:00');
DECLARE @as_of DATE = CAST(@run_start AT TIME ZONE 'UTC' AT TIME ZONE 'Romance Standard Time' AS DATE);
SELECT status,
       COUNT(*) AS n,
       SUM(CASE WHEN scheduled_at > @rs THEN 1 ELSE 0 END) AS not_started_at_run_start,
       SUM(CASE WHEN scheduled_at <= @rs AND DATEADD(MINUTE, duration_minutes, scheduled_at) > @rs THEN 1 ELSE 0 END) AS started_not_ended,
       SUM(CASE WHEN DATEADD(MINUTE, duration_minutes, scheduled_at) <= @rs AND CAST(scheduled_at AS DATE) >= @as_of THEN 1 ELSE 0 END) AS ended_on_as_of_day
FROM staging.appointment
WHERE deleted_at IS NULL AND status IN ('completed', 'no_show')
GROUP BY status;
GO

PRINT '== Q5b. Across all history: outcome stamped (updated_at) before the scheduled start or end ==';
SELECT status,
       COUNT(*) AS n,
       SUM(CASE WHEN TODATETIMEOFFSET(updated_at, '+00:00') < scheduled_at THEN 1 ELSE 0 END) AS stamped_before_start,
       SUM(CASE WHEN TODATETIMEOFFSET(updated_at, '+00:00') < DATEADD(MINUTE, duration_minutes, scheduled_at) THEN 1 ELSE 0 END) AS stamped_before_end
FROM staging.appointment
WHERE deleted_at IS NULL AND status IN ('completed', 'no_show')
GROUP BY status;
GO

PRINT '== Q5c. Up to 10 examples of an outcome on an appointment not ended at the run start ==';
DECLARE @rs DATETIMEOFFSET(0) = TODATETIMEOFFSET((SELECT MAX(started_at) FROM raw.run), '+00:00');
SELECT TOP (10) id, clinic_id, service, status, scheduled_at, duration_minutes,
       CONVERT(VARCHAR(19), updated_at, 126) AS updated_at_utc
FROM staging.appointment
WHERE deleted_at IS NULL AND status IN ('completed', 'no_show')
  AND DATEADD(MINUTE, duration_minutes, scheduled_at) > @rs
ORDER BY scheduled_at;
GO
