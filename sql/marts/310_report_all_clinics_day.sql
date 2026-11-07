-- The report's "All" row (Phase 5, D-056): one row per past day, summed over every clinic.
--
-- The day's counts are the sum of the clinic rows in marts.report_clinic_day for that day: a
-- clinic left out of that view (closed, no appointments that day) only has zeros. The build check
-- 'report_total_differs_from_rows' holds the two together.
-- no_show_rate and no_show_rate_30d follow the same rule as the clinic rows (D-021, D-053), on the
-- summed counts; the 30 days are the days before the day itself, as in the clinic rows. They count
-- every clinic's appointments in those days, including a clinic that closed within them. The window counts rows, which are days: marts.clinic_day has every date
-- of marts.dim_date, so the grouped rows below have one row per past date. days_30d is the number
-- of days of data the window covers, computed as in the clinic rows.

CREATE OR ALTER VIEW marts.report_all_clinics_day AS
WITH first_day AS (
    SELECT MIN(calendar_date) AS first_date
    FROM marts.clinic_day
    WHERE appointments > 0
),
per_day AS (
    SELECT calendar_date,
           SUM(completed)          AS completed,
           SUM(no_show)            AS no_show,
           SUM(no_show_assumed)    AS no_show_assumed,
           SUM(attended_or_missed) AS attended_or_missed
    FROM marts.clinic_day
    WHERE day_status = 'past'
    GROUP BY calendar_date
),
windowed AS (
    SELECT calendar_date,
           completed,
           no_show,
           no_show_assumed,
           attended_or_missed,
           COALESCE(SUM(no_show + no_show_assumed) OVER (
               ORDER BY calendar_date ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING), 0) AS missed_30d,
           COALESCE(SUM(attended_or_missed) OVER (
               ORDER BY calendar_date ROWS BETWEEN 30 PRECEDING AND 1 PRECEDING), 0) AS attended_or_missed_30d,
           CASE WHEN p.calendar_date <= f.first_date THEN 0
                WHEN DATEDIFF(DAY, f.first_date, p.calendar_date) < 30
                THEN DATEDIFF(DAY, f.first_date, p.calendar_date)
                ELSE 30 END                                                          AS days_30d
    FROM per_day AS p
    CROSS JOIN first_day AS f
)
SELECT calendar_date,
       completed,
       no_show,
       no_show_assumed,
       attended_or_missed,
       CASE WHEN attended_or_missed > 0
            THEN CAST(1.0 * (no_show + no_show_assumed) / attended_or_missed AS DECIMAL(9, 6))
       END                                                             AS no_show_rate,
       CAST(CASE WHEN no_show_assumed > 0 THEN 1 ELSE 0 END AS BIT)    AS is_assumed,
       missed_30d,
       attended_or_missed_30d,
       days_30d,
       CASE WHEN attended_or_missed_30d > 0
            THEN CAST(1.0 * missed_30d / attended_or_missed_30d AS DECIMAL(9, 6))
       END                                                             AS no_show_rate_30d
FROM windowed;
GO
