-- Clinic dimension: every clinic, closed ones included, with is_active. The report lists a closed
-- clinic only on the days it had appointments; totals count every clinic.

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
