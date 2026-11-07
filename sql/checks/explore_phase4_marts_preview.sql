-- Phase 4 step 1 preview (read-only): a first look at the marts views after they are created.
-- Not part of the build; compares the views with what the Q1-Q5 queries saw on 7 Nov.

SET NOCOUNT ON;
GO

PRINT '== P1. As of, per clinic ==';
SELECT COUNT(*) AS clinics, MIN(as_of_date) AS min_as_of_date, MAX(as_of_date) AS max_as_of_date,
       MIN(run_started_at_utc) AS run_started_at_utc, MIN(as_of_local) AS as_of_local
FROM marts.as_of;
GO

PRINT '== P2. Date dimension ==';
SELECT COUNT(*) AS days, MIN(calendar_date) AS first_date, MAX(calendar_date) AS last_date,
       SUM(CAST(is_sunday AS INT)) AS sundays
FROM marts.dim_date;
SELECT TOP (8) * FROM marts.dim_date WHERE calendar_date >= '2026-10-23' ORDER BY calendar_date;
GO

PRINT '== P3. Services and clinics ==';
SELECT service FROM marts.dim_service ORDER BY service;
SELECT COUNT(*) AS clinics, SUM(CAST(is_active AS INT)) AS active FROM marts.dim_clinic;
GO

PRINT '== P4. Fact: outcomes against the source status ==';
SELECT status, outcome, COUNT(*) AS n,
       SUM(CAST(cancelled_after_start AS INT)) AS cancelled_after_start
FROM marts.fact_appointment
GROUP BY status, outcome
ORDER BY status, outcome;
GO

PRINT '== P5. Fact: a few rows ==';
SELECT TOP (3) * FROM marts.fact_appointment ORDER BY scheduled_at DESC, appointment_id;
GO

PRINT '== P6. All clinics, last 7 days before the as-of date (active clinics) ==';
SELECT calendar_date, SUM(attended_or_missed) AS attended_or_missed, SUM(no_show) AS no_show,
       SUM(no_show_assumed) AS assumed, SUM(cancelled) AS cancelled,
       CAST(100.0 * SUM(no_show + no_show_assumed) / NULLIF(SUM(attended_or_missed), 0) AS DECIMAL(5, 2)) AS no_show_rate_pct,
       CAST(100.0 * SUM(no_show) / NULLIF(SUM(completed + no_show), 0) AS DECIMAL(5, 2))                  AS confirmed_rate_pct,
       SUM(CAST(is_assumed AS INT)) AS clinics_with_assumed
FROM marts.clinic_day
WHERE is_active = 1 AND day_status = 'past' AND calendar_date >= DATEADD(DAY, -7, as_of_date)
GROUP BY calendar_date
ORDER BY calendar_date;
GO

PRINT '== P7. Overall, all past days, all clinics ==';
SELECT SUM(completed) AS completed, SUM(no_show) AS no_show, SUM(no_show_assumed) AS assumed,
       CAST(100.0 * SUM(no_show) / NULLIF(SUM(completed + no_show), 0) AS DECIMAL(5, 2)) AS confirmed_rate_pct,
       CAST(100.0 * SUM(no_show + no_show_assumed) / NULLIF(SUM(attended_or_missed), 0) AS DECIMAL(5, 2)) AS no_show_rate_pct
FROM marts.clinic_day
WHERE day_status = 'past';
GO

PRINT '== P8. Top 5 active clinics by confirmed rate, all past days ==';
SELECT TOP (5) cd.clinic_id, c.clinic_name,
       CAST(100.0 * SUM(cd.no_show) / NULLIF(SUM(cd.completed + cd.no_show), 0) AS DECIMAL(5, 2)) AS confirmed_rate_pct,
       SUM(cd.completed + cd.no_show) AS completed_plus_no_show
FROM marts.clinic_day AS cd
JOIN marts.dim_clinic AS c ON c.clinic_id = cd.clinic_id
WHERE cd.is_active = 1 AND cd.day_status = 'past'
GROUP BY cd.clinic_id, c.clinic_name
ORDER BY confirmed_rate_pct DESC;
GO

PRINT '== P9. Clinic-day rows: rates present or NULL ==';
SELECT day_status,
       COUNT(*) AS clinic_days,
       SUM(CASE WHEN no_show_rate IS NULL THEN 1 ELSE 0 END) AS rate_null,
       SUM(CASE WHEN no_show_rate IS NOT NULL THEN 1 ELSE 0 END) AS rate_present
FROM marts.clinic_day
GROUP BY day_status
ORDER BY day_status;
GO
