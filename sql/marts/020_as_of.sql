-- "As of" per clinic: the start of the latest run in raw.run (highest started_at,
-- any source), converted to the clinic's own local time. as_of_date is the clinic's "today":
-- booked appointments before it are assumed no-shows, booked ones on or after it are pending.
-- A clinic whose zone is not in marts.clinic_timezone has no row here (and so no fact rows);
-- the build checks catch that.

CREATE OR ALTER VIEW marts.as_of AS
WITH latest AS (
    SELECT MAX(started_at) AS run_started_at_utc
    FROM raw.run
)
SELECT c.id AS clinic_id,
       l.run_started_at_utc,
       (l.run_started_at_utc AT TIME ZONE 'UTC') AT TIME ZONE z.windows_name AS as_of_local,
       CAST((l.run_started_at_utc AT TIME ZONE 'UTC') AT TIME ZONE z.windows_name AS DATE) AS as_of_date
FROM staging.clinic AS c
JOIN marts.clinic_timezone AS z ON z.iana_name = c.timezone
CROSS JOIN latest AS l
WHERE l.run_started_at_utc IS NOT NULL;
GO
