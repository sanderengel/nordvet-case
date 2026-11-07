-- Phase 4 regression against the saved pull P3 (D-038): the marts against the numbers verified
-- in Phase 2 (table "Documented numbers" in phase-4-questions.md). Reads only; writes
-- nothing but a temp table for the summary.
--
-- Run in a scratch database that holds only the P3 replay (as of 6 Nov 17:57 local), never in nordvet:
--   MSSQL_DATABASE=nordvet_p3 .venv/bin/python -m nordvet.ingest.replay --pull data/raw/20261106T174032Z_pull --reset
--   MSSQL_DATABASE=nordvet_p3 .venv/bin/python -m nordvet.model.build
--   docker exec -i nordvet-sql bash -c '/opt/mssql-tools18/bin/sqlcmd -C -I -S localhost -U sa -P "$MSSQL_SA_PASSWORD" -d nordvet_p3 -W -s "|"' < sql/checks/regression_p3.sql
--
-- The documented numbers came from the Python profile scripts (profile_raw, profile_recording)
-- on the same pull, with "today" = 6 Nov. Definitions are copied from them:
--   * status counts use the source status, split at scheduled local date 6 Nov;
--   * rates are confirmed rates, no_show / (completed + no_show), over dates before 6 Nov;
--     the clinic range is over all 39 clinics, the closed one included;
--   * cancellation timing is (scheduled start - cancelled_at) in seconds: below 0 "after the start",
--     then under 2 h, 2-24 h, 1-7 days, 7+ days (profile_raw.lead_bucket). D-022's 984 is
--     "at or after the start" (profile_recording), the model's cancelled_after_start; both are shown.
-- Visible and soft-deleted counts in Phase 2 came from two fetches (with and without deleted rows)
-- a short time apart; the replay uses only the fetch with deleted rows, so small differences there
-- would be timing, not a model error.

SET NOCOUNT ON;
GO

WITH f AS (
    SELECT * FROM marts.fact_appointment
),
past AS (
    SELECT * FROM f WHERE scheduled_date < '2026-11-06'
),
ahead AS (
    SELECT * FROM f WHERE scheduled_date >= '2026-11-06'
),
clinic_rate AS (
    SELECT clinic_id,
           100.0 * SUM(CASE WHEN status = 'no_show' THEN 1 ELSE 0 END)
                 / NULLIF(SUM(CASE WHEN status IN ('completed', 'no_show') THEN 1 ELSE 0 END), 0) AS pct
    FROM past
    GROUP BY clinic_id
),
clinic_ranked AS (
    SELECT clinic_id, pct, ROW_NUMBER() OVER (ORDER BY pct DESC) AS rn FROM clinic_rate
),
service_rate AS (
    SELECT service,
           100.0 * SUM(CASE WHEN status = 'no_show' THEN 1 ELSE 0 END)
                 / NULLIF(SUM(CASE WHEN status IN ('completed', 'no_show') THEN 1 ELSE 0 END), 0) AS pct
    FROM past
    GROUP BY service
),
cancel AS (
    SELECT cancelled_after_start,
           DATEDIFF(SECOND, TODATETIMEOFFSET(cancelled_at_utc, '+00:00'), scheduled_at) AS seconds_before
    FROM f
    WHERE status = 'cancelled'
),
cancel_bucket AS (
    SELECT CASE WHEN seconds_before < 0      THEN 'after the start'
                WHEN seconds_before < 7200   THEN 'under 2 h before'
                WHEN seconds_before < 86400  THEN '2-24 h before'
                WHEN seconds_before < 604800 THEN '1-7 days before'
                ELSE '7+ days before' END AS bucket
    FROM cancel
),
result (ord, item, documented, model) AS (
    SELECT 1, 'as-of date of every clinic', '2026-11-06',
           CONCAT(MIN(as_of_date), CASE WHEN MIN(as_of_date) <> MAX(as_of_date) THEN CONCAT(' .. ', MAX(as_of_date)) END)
    FROM marts.as_of
    UNION ALL SELECT 2, 'visible (non-deleted) appointments', '65177', CONCAT(COUNT(*), '') FROM f
    UNION ALL SELECT 3, 'soft-deleted appointments', '934',
           CONCAT(COUNT(*), '') FROM staging.appointment WHERE deleted_at IS NOT NULL
    UNION ALL SELECT 4, 'before 6 Nov: completed', '50900', CONCAT(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), '') FROM past
    UNION ALL SELECT 5, 'before 6 Nov: cancelled', '5039',  CONCAT(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), '') FROM past
    UNION ALL SELECT 6, 'before 6 Nov: no_show',   '4869',  CONCAT(SUM(CASE WHEN status = 'no_show'   THEN 1 ELSE 0 END), '') FROM past
    UNION ALL SELECT 7, 'before 6 Nov: booked',    '38',    CONCAT(SUM(CASE WHEN status = 'booked'    THEN 1 ELSE 0 END), '') FROM past
    UNION ALL SELECT 8, 'before 6 Nov: booked = no_show_assumed', '38',
           CONCAT(SUM(CASE WHEN outcome = 'no_show_assumed' THEN 1 ELSE 0 END), '') FROM past
    UNION ALL SELECT 9,  '6 Nov and later: booked',    '3607', CONCAT(SUM(CASE WHEN status = 'booked'    THEN 1 ELSE 0 END), '') FROM ahead
    UNION ALL SELECT 10, '6 Nov and later: completed', '591',  CONCAT(SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END), '') FROM ahead
    UNION ALL SELECT 11, '6 Nov and later: cancelled', '133',  CONCAT(SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END), '') FROM ahead
    UNION ALL SELECT 12, '6 Nov and later: no_show',   '0',    CONCAT(SUM(CASE WHEN status = 'no_show'   THEN 1 ELSE 0 END), '') FROM ahead
    UNION ALL SELECT 13, 'confirmed rate before 6 Nov (no_show / completed+no_show)', '4869 / 55769 = 8.73%',
           CONCAT(SUM(CASE WHEN status = 'no_show' THEN 1 ELSE 0 END), ' / ',
                  SUM(CASE WHEN status IN ('completed', 'no_show') THEN 1 ELSE 0 END), ' = ',
                  CAST(100.0 * SUM(CASE WHEN status = 'no_show' THEN 1 ELSE 0 END)
                       / NULLIF(SUM(CASE WHEN status IN ('completed', 'no_show') THEN 1 ELSE 0 END), 0) AS DECIMAL(6, 2)), '%')
    FROM past
    UNION ALL SELECT 14, 'assumed rate before 6 Nov from marts.clinic_day (derived: 4907 / 55807)', '8.79%',
           CONCAT(CAST(100.0 * SUM(no_show + no_show_assumed) / NULLIF(SUM(attended_or_missed), 0) AS DECIMAL(6, 2)), '%')
    FROM marts.clinic_day WHERE day_status = 'past'
    UNION ALL SELECT 15, 'confirmed rate before 6 Nov from marts.clinic_day', '8.73%',
           CONCAT(CAST(100.0 * SUM(no_show) / NULLIF(SUM(completed + no_show), 0) AS DECIMAL(6, 2)), '%')
    FROM marts.clinic_day WHERE day_status = 'past'
    UNION ALL SELECT 16, 'clinic rank 1', 'CL-507 22.5%', CONCAT(clinic_id, ' ', CAST(pct AS DECIMAL(5, 1)), '%') FROM clinic_ranked WHERE rn = 1
    UNION ALL SELECT 17, 'clinic rank 2', 'CL-384 21.6%', CONCAT(clinic_id, ' ', CAST(pct AS DECIMAL(5, 1)), '%') FROM clinic_ranked WHERE rn = 2
    UNION ALL SELECT 18, 'clinic rank 3', 'CL-930 18.5%', CONCAT(clinic_id, ' ', CAST(pct AS DECIMAL(5, 1)), '%') FROM clinic_ranked WHERE rn = 3
    UNION ALL SELECT 19, 'clinic rank 4 (rate only)', '12.4%', CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM clinic_ranked WHERE rn = 4
    UNION ALL SELECT 20, 'clinic range (all 39)', '4.3% .. 22.5%',
           CONCAT(CAST(MIN(pct) AS DECIMAL(5, 1)), '% .. ', CAST(MAX(pct) AS DECIMAL(5, 1)), '%') FROM clinic_rate
    UNION ALL SELECT 21, 'service follow_up',    '13.7%', CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM service_rate WHERE service = 'follow_up'
    UNION ALL SELECT 22, 'service vaccination',  '10.4%', CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM service_rate WHERE service = 'vaccination'
    UNION ALL SELECT 23, 'service consultation', '8.2%',  CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM service_rate WHERE service = 'consultation'
    UNION ALL SELECT 24, 'service dental',       '5.9%',  CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM service_rate WHERE service = 'dental'
    UNION ALL SELECT 25, 'service surgery',      '2.9%',  CONCAT(CAST(pct AS DECIMAL(5, 1)), '%') FROM service_rate WHERE service = 'surgery'
    UNION ALL SELECT 26, 'cancellations, all dates', '5172', CONCAT(COUNT(*), '') FROM cancel
    UNION ALL SELECT 27, 'cancelled at or after the start (model flag, D-022)', '984',
           CONCAT(SUM(CAST(cancelled_after_start AS INT)), '') FROM cancel
    UNION ALL SELECT 28, 'cancelled after the start (strictly, profile_raw bucket)', '984',
           CONCAT(SUM(CASE WHEN bucket = 'after the start' THEN 1 ELSE 0 END), '') FROM cancel_bucket
    UNION ALL SELECT 29, 'cancelled under 2 h before',  '448',  CONCAT(SUM(CASE WHEN bucket = 'under 2 h before' THEN 1 ELSE 0 END), '') FROM cancel_bucket
    UNION ALL SELECT 30, 'cancelled 2-24 h before',     '1206', CONCAT(SUM(CASE WHEN bucket = '2-24 h before'    THEN 1 ELSE 0 END), '') FROM cancel_bucket
    UNION ALL SELECT 31, 'cancelled 1-7 days before',   '2066', CONCAT(SUM(CASE WHEN bucket = '1-7 days before'  THEN 1 ELSE 0 END), '') FROM cancel_bucket
    UNION ALL SELECT 32, 'cancelled 7+ days before',    '468',  CONCAT(SUM(CASE WHEN bucket = '7+ days before'   THEN 1 ELSE 0 END), '') FROM cancel_bucket
    UNION ALL SELECT 33, 'closed clinic CL-420: appointments', '755', CONCAT(COUNT(*), '') FROM f WHERE clinic_id = 'CL-420'
    UNION ALL SELECT 34, 'closed clinic CL-420: first .. last date', '2026-08-06 .. 2026-09-10',
           CONCAT(MIN(scheduled_date), ' .. ', MAX(scheduled_date)) FROM f WHERE clinic_id = 'CL-420'
    UNION ALL SELECT 35, 'closed clinic CL-420: is_active', '0',
           CONCAT(CAST(is_active AS INT), '') FROM marts.dim_clinic WHERE clinic_id = 'CL-420'
)
SELECT ord, item, documented, model, CASE WHEN documented = model THEN 'yes' ELSE 'NO' END AS match
INTO #result  -- a temp table, so the summary below can read the same rows; gone when the session ends
FROM result;

SELECT ord, item, documented, model, match FROM #result ORDER BY ord;

SELECT CONCAT(SUM(CASE WHEN match = 'yes' THEN 1 ELSE 0 END), ' of ', COUNT(*), ' match') AS summary
FROM #result;
GO
