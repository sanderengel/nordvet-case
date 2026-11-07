-- Live reconciliation (D-028, Q12), part 2: every appointment that differs between the
-- incremental database (nordvet) and the fresh full load (nordvet_fresh).
--
-- A difference is explained only when the fresh load holds a newer version that was written
-- after the last incremental run began (a stamp can be up to 59 s earlier than the write, so 60 s
-- of slack). Anything else is a bug: a row missing on either side, or a different row with an
-- equal or older stamp. Result columns: kind, id, the status and updated_at on each side,
-- explained (1 or 0). One row per difference, unexplained first.
DECLARE @cut DATETIME2(0) = (SELECT MAX(started_at) FROM nordvet.raw.run WHERE source = 'api');
DECLARE @limit DATETIME2(0) = DATEADD(SECOND, -60, @cut);

WITH d AS (
    SELECT COALESCE(i.id, f.id) AS id,
           CASE WHEN f.id IS NULL THEN 'only_incremental'
                WHEN i.id IS NULL THEN 'only_fresh'
                ELSE 'differs' END AS kind,
           i.status AS inc_status, f.status AS fresh_status,
           i.updated_at AS inc_updated_at, f.updated_at AS fresh_updated_at
    FROM nordvet.staging.appointment AS i
    FULL OUTER JOIN nordvet_fresh.staging.appointment AS f ON f.id = i.id
    WHERE f.id IS NULL
       OR i.id IS NULL
       OR EXISTS (
            SELECT i.clinic_id, i.vet_id, i.service, i.scheduled_at, i.duration_minutes, i.status,
                   i.created_at, i.updated_at, i.cancelled_at, i.deleted_at
            EXCEPT
            SELECT f.clinic_id, f.vet_id, f.service, f.scheduled_at, f.duration_minutes, f.status,
                   f.created_at, f.updated_at, f.cancelled_at, f.deleted_at
       )
)
SELECT kind, id, inc_status, fresh_status,
       CONVERT(VARCHAR(19), inc_updated_at, 126) AS inc_updated_at,
       CONVERT(VARCHAR(19), fresh_updated_at, 126) AS fresh_updated_at,
       CASE WHEN kind = 'only_fresh' AND fresh_updated_at >= @limit THEN 1
            WHEN kind = 'differs' AND fresh_updated_at > inc_updated_at AND fresh_updated_at >= @limit THEN 1
            ELSE 0 END AS explained
FROM d
ORDER BY explained, kind, id;
