-- Clinic dimension: every clinic, closed ones included (D-039); the report leaves inactive
-- clinics out of the per-clinic view (D-016), the aggregates keep them.

CREATE OR ALTER VIEW marts.dim_clinic AS
SELECT id        AS clinic_id,
       name      AS clinic_name,
       city,
       country,
       timezone,
       active    AS is_active,
       opened_on
FROM staging.clinic;
GO
