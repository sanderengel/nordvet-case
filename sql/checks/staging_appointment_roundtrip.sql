-- Every staging.appointment row as plain text, for comparing with the source JSON in Python.
-- scheduled_at comes back as two columns, the local wall time and the offset in minutes, because
-- CONVERT style 127 would print a datetimeoffset converted to UTC. The UTC stamps use style 127
-- (ISO 8601 without the Z, since a datetime2 has no offset).

SELECT id, clinic_id, vet_id, service,
       CONVERT(VARCHAR(19), CAST(scheduled_at AS DATETIME2(0)), 126),
       DATEPART(TZOFFSET, scheduled_at),
       duration_minutes, status,
       CONVERT(VARCHAR(19), created_at, 127),
       CONVERT(VARCHAR(19), updated_at, 127),
       CONVERT(VARCHAR(19), cancelled_at, 127),
       CONVERT(VARCHAR(19), deleted_at, 127)
FROM staging.appointment;
