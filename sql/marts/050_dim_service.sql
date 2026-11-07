-- Service dimension: the services that occur in the (non-deleted) appointments.

CREATE OR ALTER VIEW marts.dim_service AS
SELECT DISTINCT service
FROM staging.appointment
WHERE deleted_at IS NULL;
GO
