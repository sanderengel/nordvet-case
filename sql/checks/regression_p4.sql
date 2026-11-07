-- Phase 4 regression against the saved pull P4 (D-038, block 2 of "Documented numbers" in
-- phase-4-questions.md): the overnight snapshot, taken before any no-show for Friday could be
-- recorded (D-021). Reads only; writes nothing but a temp table for the summary.
--
-- Run in a scratch database that holds only the P4 replay, never in nordvet. The replay must
-- stamp the run with the pull's own start (its first all-rows page, 6 Nov 23:28 UTC = 7 Nov 00:28 local; F1, fixed in Phase 4):
--   MSSQL_DATABASE=nordvet_p4 .venv/bin/python -m nordvet.ingest.replay --pull data/raw/20261106T232703Z_pull --reset
--   MSSQL_DATABASE=nordvet_p4 .venv/bin/python -m nordvet.model.build
--   docker exec -i nordvet-sql bash -c '/opt/mssql-tools18/bin/sqlcmd -C -I -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -d nordvet_p4 -W -s "|"' < sql/checks/regression_p4.sql
--
-- The documented numbers came from profile_recording (check 3, the daily table) with today = 7 Nov:
-- per scheduled local date, over all clinics, completed / no_show / still booked;
-- confirmed = no_show / (completed + no_show), assumed = (no_show + booked) / (completed + no_show + booked).
-- The model gives the same through marts.clinic_day (booked before the as-of date = no_show_assumed).

SET NOCOUNT ON;
GO

WITH day AS (
    SELECT calendar_date,
           SUM(completed) AS completed, SUM(no_show) AS no_show, SUM(no_show_assumed) AS assumed,
           SUM(pending) AS pending
    FROM marts.clinic_day
    WHERE calendar_date IN ('2026-11-05', '2026-11-06')
    GROUP BY calendar_date
),
fri AS (SELECT * FROM day WHERE calendar_date = '2026-11-06'),
thu AS (SELECT * FROM day WHERE calendar_date = '2026-11-05'),
result (ord, item, documented, model) AS (
    SELECT 1, 'as-of date of every clinic', '2026-11-07',
           CONCAT(MIN(as_of_date), CASE WHEN MIN(as_of_date) <> MAX(as_of_date) THEN CONCAT(' .. ', MAX(as_of_date)) END)
    FROM marts.as_of
    -- 00:28, not 00:27: the pull folder is named after its first request (23:27:03 UTC, the variant
    -- without deleted rows); the replay reads only the variant with deleted rows, whose first page
    -- came at 23:28:05 UTC (checked 7 Nov, step 4). The as-of date is the same.
    UNION ALL SELECT 2, 'as-of local time (hh:mm), first all-rows page', '00:28', CONVERT(CHAR(5), CAST(MIN(as_of_local) AS TIME), 108) FROM marts.as_of
    UNION ALL SELECT 3, 'soft-deleted appointments (D-015, 00:27 pull)', '936',
           CONCAT(COUNT(*), '') FROM staging.appointment WHERE deleted_at IS NOT NULL
    UNION ALL SELECT 4,  'Fri 6 Nov: completed',             '645',  CONCAT(completed, '') FROM fri
    UNION ALL SELECT 5,  'Fri 6 Nov: no_show (confirmed)',   '0',    CONCAT(no_show, '')   FROM fri
    UNION ALL SELECT 6,  'Fri 6 Nov: still booked = assumed', '58',  CONCAT(assumed, '')   FROM fri
    UNION ALL SELECT 7,  'Fri 6 Nov: pending',               '0',    CONCAT(pending, '')   FROM fri
    UNION ALL SELECT 8,  'Fri 6 Nov: confirmed rate',        '0.0%',
           CONCAT(CAST(100.0 * no_show / NULLIF(completed + no_show, 0) AS DECIMAL(5, 1)), '%') FROM fri
    UNION ALL SELECT 9,  'Fri 6 Nov: assumed rate',          '8.3%',
           CONCAT(CAST(100.0 * (no_show + assumed) / NULLIF(completed + no_show + assumed, 0) AS DECIMAL(5, 1)), '%') FROM fri
    UNION ALL SELECT 10, 'Thu 5 Nov: no_show (confirmed)',   '53',   CONCAT(no_show, '')   FROM thu
    UNION ALL SELECT 11, 'Thu 5 Nov: still booked = assumed', '21',  CONCAT(assumed, '')   FROM thu
    UNION ALL SELECT 12, 'Thu 5 Nov: assumed rate',          '9.5%',
           CONCAT(CAST(100.0 * (no_show + assumed) / NULLIF(completed + no_show + assumed, 0) AS DECIMAL(5, 1)), '%') FROM thu
    UNION ALL SELECT 13, 'Fri 6 Nov: clinics marked assumed', '(not documented)',
           CONCAT(SUM(CAST(is_assumed AS INT)), ' of ', COUNT(*), ' clinics')
    FROM marts.clinic_day WHERE calendar_date = '2026-11-06'
)
SELECT ord, item, documented, model,
       CASE WHEN documented = '(not documented)' THEN 'info' WHEN documented = model THEN 'yes' ELSE 'NO' END AS match
INTO #result
FROM result;

SELECT ord, item, documented, model, match FROM #result ORDER BY ord;

SELECT CONCAT(SUM(CASE WHEN match = 'yes' THEN 1 ELSE 0 END), ' of ', SUM(CASE WHEN match <> 'info' THEN 1 ELSE 0 END), ' match') AS summary
FROM #result;
GO
