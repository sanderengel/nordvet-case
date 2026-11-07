-- Fact table: one row per non-deleted appointment (soft-deleted rows, which the source has
-- withdrawn, are left out).
-- No vet_id: vets are double-booked, so it may not be who did the work, and there are no
-- vet-level metrics. No patient_ref: it is kept only in raw.
--
-- outcome, derived; the source status itself is kept unchanged in `status`:
--   completed, no_show, cancelled  as recorded by the clinic, whatever the date
--   no_show_assumed                still booked, scheduled before the clinic's as-of date
--   pending                        still booked, scheduled on or after the as-of date
--   other                          a status value the loader has not seen
--
-- cancelled_after_start: a cancellation recorded at or after the scheduled start (an ordinary
-- cancellation entered late by staff, never counted as a no-show). The UTC
-- stamps are DATETIME2; TODATETIMEOFFSET(..., '+00:00') makes the comparison with the local
-- scheduled_at explicit (checked live: the same result as the implicit comparison).
-- Minute differences between a DATETIMEOFFSET and a UTC stamp are taken in UTC by DATEDIFF.

CREATE OR ALTER VIEW marts.fact_appointment AS
SELECT a.id                                                      AS appointment_id,
       a.clinic_id,
       a.service,
       CAST(a.scheduled_at AS DATE)                              AS scheduled_date,   -- local date
       a.scheduled_at,                                                                -- local time with offset
       DATEADD(MINUTE, a.duration_minutes, a.scheduled_at)       AS scheduled_end_at,
       a.duration_minutes,
       a.status,
       CASE
           WHEN a.status = 'completed' THEN 'completed'
           WHEN a.status = 'no_show'   THEN 'no_show'
           WHEN a.status = 'cancelled' THEN 'cancelled'
           WHEN a.status = 'booked' AND CAST(a.scheduled_at AS DATE) < o.as_of_date THEN 'no_show_assumed'
           WHEN a.status = 'booked'    THEN 'pending'
           ELSE 'other'
       END                                                       AS outcome,
       CAST(CASE WHEN a.status = 'cancelled'
                  AND TODATETIMEOFFSET(a.cancelled_at, '+00:00') >= a.scheduled_at
                 THEN 1 ELSE 0 END AS BIT)                       AS cancelled_after_start,
       a.created_at                                              AS created_at_utc,
       a.cancelled_at                                            AS cancelled_at_utc,
       DATEDIFF(MINUTE, TODATETIMEOFFSET(a.created_at, '+00:00'), a.scheduled_at) AS booking_lead_minutes,
       CASE WHEN a.status = 'cancelled'
            THEN DATEDIFF(MINUTE, TODATETIMEOFFSET(a.cancelled_at, '+00:00'), a.scheduled_at)
       END                                                       AS cancel_notice_minutes,  -- negative: after the start
       o.as_of_date
FROM staging.appointment AS a
JOIN marts.as_of AS o ON o.clinic_id = a.clinic_id
WHERE a.deleted_at IS NULL;
GO
