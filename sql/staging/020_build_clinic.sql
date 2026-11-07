-- Rebuild staging.clinic from the latest clinics response in raw (39 rows; full refresh, Q16).

TRUNCATE TABLE staging.clinic;
GO

INSERT INTO staging.clinic (id, name, city, country, timezone, active, opened_on)
SELECT j.id, j.name, j.city, j.country, j.timezone, j.active, j.opened_on
FROM (SELECT TOP (1) body FROM raw.page WHERE endpoint = 'clinics' ORDER BY page_id DESC) AS p
CROSS APPLY OPENJSON(p.body, N'$.data') WITH (
    id        VARCHAR(50)   N'$.id',
    name      NVARCHAR(100) N'$.name',
    city      NVARCHAR(100) N'$.city',
    country   CHAR(2)       N'$.country',
    timezone  VARCHAR(40)   N'$.timezone',
    active    BIT           N'$.active',
    opened_on DATE          N'$.opened_on'
) AS j;
GO
