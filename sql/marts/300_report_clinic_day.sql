-- What the report reads (Phase 5, D-047, D-050, D-053, D-055): one row per clinic per past day,
-- with the day's counts and rate from marts.clinic_day plus the rate over the 30 days before it.
--
-- Which clinics: every open clinic on every past day (an empty day shows N/A), and a closed
-- clinic on the days it had appointments, so its history is shown where it happened (D-055,
-- replacing the per-clinic rule of D-016). Past days only: the as-of day and later have no
-- rate (D-021).
--
-- no_show_rate_30d: the same assumed rate as no_show_rate (D-021), over the 30 calendar days
-- before calendar_date (the day itself is left out, so the column is a normal to compare the
-- day against):
--   SUM(no_show + no_show_assumed) / SUM(completed + no_show + no_show_assumed)
-- NULL when those days hold nothing to divide (D-039). The window counts rows, which are days,
-- because marts.clinic_day has exactly one row per clinic per date of marts.dim_date. It is taken
-- before the filter on past days, but a window that ends before a past day only holds past days.
-- days_30d is the number of days of data the window covers: 30, or fewer for the days less than
-- 31 days after the first day with any appointment (6 Aug 2026 in the live data; the date
-- dimension starts earlier, on 1 Aug, D-040), and 0 on that first day and before it. The report's
-- header uses it, so the column never claims more days than there is data for.

CREATE OR ALTER VIEW marts.report_clinic_day AS
WITH windowed AS (
    SELECT d.clinic_id,
           d.calendar_date,
           d.is_active,
           d.day_status,
           d.as_of_date,
           d.appointments,
           d.completed,
           d.no_show,
           d.no_show_assumed,
           d.attended_or_missed,
           d.no_show_rate,
           d.is_assumed,
           COALESCE(SUM(d.no_show + d.no_show_assumed) OVER (
               PARTITION BY d.clinic_id ORDER BY d.calendar_date
               ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING), 0)        AS missed_30d,
           COALESCE(SUM(d.attended_or_missed) OVER (
               PARTITION BY d.clinic_id ORDER BY d.calendar_date
               ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING), 0)        AS attended_or_missed_30d,
           CASE WHEN d.calendar_date <= f.first_date THEN 0
                WHEN DATEDIFF(DAY, f.first_date, d.calendar_date) < 30
                THEN DATEDIFF(DAY, f.first_date, d.calendar_date)
                ELSE 30 END                                          AS days_30d
    FROM marts.clinic_day AS d
    CROSS JOIN (SELECT MIN(calendar_date) AS first_date
                FROM marts.clinic_day WHERE appointments > 0) AS f
)
SELECT w.clinic_id,
       c.clinic_name,
       c.country,
       w.is_active,
       w.calendar_date,
       w.completed,
       w.no_show,
       w.no_show_assumed,
       w.attended_or_missed,
       w.no_show_rate,
       w.is_assumed,
       w.missed_30d,
       w.attended_or_missed_30d,
       w.days_30d,
       CASE WHEN w.attended_or_missed_30d > 0
            THEN CAST(1.0 * w.missed_30d / w.attended_or_missed_30d AS DECIMAL(9, 6))
       END                                                          AS no_show_rate_30d,
       w.as_of_date,
       o.as_of_local
FROM windowed AS w
JOIN marts.dim_clinic AS c ON c.clinic_id = w.clinic_id
JOIN marts.as_of AS o ON o.clinic_id = w.clinic_id
WHERE (w.is_active = 1 OR w.appointments > 0)
  AND w.day_status = 'past';
GO
