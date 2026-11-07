-- IANA time-zone names (as the API gives them in staging.clinic.timezone) mapped to the Windows
-- names that SQL Server's AT TIME ZONE accepts (D-042). AT TIME ZONE rejects IANA names
-- (error 9820, checked live 7 Nov). A clinic in a new zone needs one more line here; until then
-- the build check 'clinic_zone_unmapped' fails.

CREATE OR ALTER VIEW marts.clinic_timezone AS
SELECT iana_name, windows_name
FROM (VALUES
    (CAST('Europe/Copenhagen' AS VARCHAR(40)), CAST(N'Romance Standard Time' AS NVARCHAR(128))),
    ('Europe/Stockholm', N'W. Europe Standard Time')
) AS m (iana_name, windows_name);
GO
