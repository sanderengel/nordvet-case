-- One row per clinic per calendar date (every clinic x every date of marts.dim_date), with the
-- counts per outcome and the two rates. This is what the report reads.
--
-- no_show_rate (D-021): (no_show + no_show_assumed) / (completed + no_show + no_show_assumed)
-- confirmed_no_show_rate (D-020): no_show / (completed + no_show)
-- Both are NULL when there is nothing to divide (D-039) and for the as-of day and later, which
-- are not shown as a rate (D-021). is_assumed = 1 while the day still has assumed no-shows.
-- Closed clinics are kept (is_active = 0); the report filters them (D-016, D-039).

CREATE OR ALTER VIEW marts.clinic_day AS
WITH counts AS (
    SELECT clinic_id,
           scheduled_date,
           COUNT(*)                                                     AS appointments,
           SUM(CASE WHEN outcome = 'completed'       THEN 1 ELSE 0 END) AS completed,
           SUM(CASE WHEN outcome = 'no_show'         THEN 1 ELSE 0 END) AS no_show,
           SUM(CASE WHEN outcome = 'no_show_assumed' THEN 1 ELSE 0 END) AS no_show_assumed,
           SUM(CASE WHEN outcome = 'cancelled'       THEN 1 ELSE 0 END) AS cancelled,
           SUM(CASE WHEN outcome = 'pending'         THEN 1 ELSE 0 END) AS pending,
           SUM(CASE WHEN outcome = 'other'           THEN 1 ELSE 0 END) AS other,
           SUM(CASE WHEN cancelled_after_start = 1   THEN 1 ELSE 0 END) AS cancelled_after_start
    FROM marts.fact_appointment
    GROUP BY clinic_id, scheduled_date
)
SELECT c.clinic_id,
       d.calendar_date,
       c.is_active,
       o.as_of_date,
       CASE WHEN d.calendar_date < o.as_of_date THEN 'past'
            WHEN d.calendar_date = o.as_of_date THEN 'as_of_day'
            ELSE 'future' END                                          AS day_status,
       z.appointments,
       z.completed,
       z.no_show,
       z.no_show_assumed,
       z.cancelled,
       z.cancelled_after_start,
       z.pending,
       z.other,
       z.completed + z.no_show + z.no_show_assumed                    AS attended_or_missed,
       CASE WHEN d.calendar_date < o.as_of_date AND z.completed + z.no_show + z.no_show_assumed > 0
            THEN CAST(1.0 * (z.no_show + z.no_show_assumed) / (z.completed + z.no_show + z.no_show_assumed) AS DECIMAL(9, 6))
       END                                                            AS no_show_rate,
       CASE WHEN d.calendar_date < o.as_of_date AND z.completed + z.no_show > 0
            THEN CAST(1.0 * z.no_show / (z.completed + z.no_show) AS DECIMAL(9, 6))
       END                                                            AS confirmed_no_show_rate,
       CAST(CASE WHEN z.no_show_assumed > 0 THEN 1 ELSE 0 END AS BIT) AS is_assumed
FROM marts.dim_clinic AS c
CROSS JOIN marts.dim_date AS d
JOIN marts.as_of AS o ON o.clinic_id = c.clinic_id
LEFT JOIN counts AS k ON k.clinic_id = c.clinic_id AND k.scheduled_date = d.calendar_date
CROSS APPLY (SELECT COALESCE(k.appointments, 0)          AS appointments,
                    COALESCE(k.completed, 0)             AS completed,
                    COALESCE(k.no_show, 0)               AS no_show,
                    COALESCE(k.no_show_assumed, 0)       AS no_show_assumed,
                    COALESCE(k.cancelled, 0)             AS cancelled,
                    COALESCE(k.cancelled_after_start, 0) AS cancelled_after_start,
                    COALESCE(k.pending, 0)               AS pending,
                    COALESCE(k.other, 0)                 AS other) AS z;
GO
