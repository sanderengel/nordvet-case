-- Build checks for the marts (D-038, D-042). Read-only: one result set, one row per check.
-- severity 'fail': the build fails when problems > 0. severity 'info': reported, never fails.

SELECT CAST('clinic_zone_unmapped' AS VARCHAR(60)) AS check_name,
       CAST('fail' AS VARCHAR(4))                  AS severity,
       COUNT(*)                                    AS problems,
       CAST(N'clinics whose time zone is not in marts.clinic_timezone (D-042)' AS NVARCHAR(200)) AS note
FROM staging.clinic AS c
WHERE NOT EXISTS (SELECT 1 FROM marts.clinic_timezone AS z WHERE z.iana_name = c.timezone)

UNION ALL
SELECT 'no_run_in_raw', 'fail',
       CASE WHEN EXISTS (SELECT 1 FROM raw.run) THEN 0 ELSE 1 END,
       N'no run in raw.run, so there is no as-of date (D-035)'

UNION ALL
SELECT 'fact_rows_differ_from_staging', 'fail',
       ABS((SELECT COUNT(*) FROM marts.fact_appointment)
           - (SELECT COUNT(*) FROM staging.appointment WHERE deleted_at IS NULL)),
       N'fact rows against non-deleted staging rows (D-036)'

UNION ALL
SELECT 'duplicate_appointment_id', 'fail', COUNT(*),
       N'appointment ids that occur more than once in the fact'
FROM (SELECT appointment_id FROM marts.fact_appointment GROUP BY appointment_id HAVING COUNT(*) > 1) AS x

UNION ALL
SELECT 'outcome_missing', 'fail', COUNT(*),
       N'fact rows without an outcome (D-021, D-043)'
FROM marts.fact_appointment
WHERE outcome IS NULL

UNION ALL
SELECT 'assumed_not_in_past', 'fail', COUNT(*),
       N'assumed no-shows on or after the as-of date (D-021)'
FROM marts.fact_appointment
WHERE outcome = 'no_show_assumed' AND scheduled_date >= as_of_date

UNION ALL
SELECT 'appointment_outside_date_dimension', 'fail', COUNT(*),
       N'fact rows whose date is not in marts.dim_date (D-040)'
FROM marts.fact_appointment AS f
WHERE NOT EXISTS (SELECT 1 FROM marts.dim_date AS d WHERE d.calendar_date = f.scheduled_date)

UNION ALL
SELECT 'clinic_day_total_differs_from_fact', 'fail',
       ABS(COALESCE((SELECT SUM(appointments) FROM marts.clinic_day), 0)
           - (SELECT COUNT(*) FROM marts.fact_appointment)),
       N'appointments summed over marts.clinic_day against fact rows'

UNION ALL
SELECT 'rate_out_of_range', 'fail', COUNT(*),
       N'clinic-day rates below 0 or above 1'
FROM marts.clinic_day
WHERE no_show_rate < 0 OR no_show_rate > 1 OR confirmed_no_show_rate < 0 OR confirmed_no_show_rate > 1

UNION ALL
SELECT 'soft_deleted_excluded', 'info', 0,
       CONCAT(COUNT(*), N' soft-deleted appointments left out of the fact (D-015)')
FROM staging.appointment
WHERE deleted_at IS NOT NULL

UNION ALL
SELECT 'status_not_known', 'info', 0,
       CONCAT(COUNT(*), N' fact rows with outcome ''other'' (an unseen status, D-023)')
FROM marts.fact_appointment
WHERE outcome = 'other';
GO
