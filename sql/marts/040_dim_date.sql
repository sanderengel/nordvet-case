-- Date dimension: every calendar date from 2026-08-01 (the first month with data)
-- to the last appointment date in staging. Weekday numbers follow ISO (1 = Monday) and do not
-- depend on the server's DATEFIRST or language settings (1900-01-01 was a Monday).
-- The number generator covers 10,000 days (about 27 years).

CREATE OR ALTER VIEW marts.dim_date AS
WITH digits AS (
    SELECT d FROM (VALUES (0), (1), (2), (3), (4), (5), (6), (7), (8), (9)) AS v (d)
),
numbers AS (
    SELECT a.d + 10 * b.d + 100 * c.d + 1000 * e.d AS n
    FROM digits AS a CROSS JOIN digits AS b CROSS JOIN digits AS c CROSS JOIN digits AS e
),
bounds AS (
    SELECT CAST('2026-08-01' AS DATE) AS first_date,
           MAX(CAST(scheduled_at AS DATE)) AS last_date
    FROM staging.appointment
),
dates AS (
    SELECT DATEADD(DAY, n.n, b.first_date) AS calendar_date
    FROM numbers AS n
    CROSS JOIN bounds AS b
    WHERE n.n <= DATEDIFF(DAY, b.first_date, b.last_date)
),
numbered AS (
    SELECT calendar_date,
           DATEDIFF(DAY, CAST('1900-01-01' AS DATE), calendar_date) % 7 + 1 AS iso_weekday
    FROM dates
)
SELECT calendar_date,
       iso_weekday,
       CHOOSE(iso_weekday, 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday') AS weekday_name,
       DATEPART(ISO_WEEK, calendar_date)                         AS iso_week,
       YEAR(DATEADD(DAY, 4 - iso_weekday, calendar_date))        AS iso_year,  -- year of that week's Thursday
       YEAR(calendar_date)                                       AS calendar_year,
       MONTH(calendar_date)                                      AS calendar_month,
       CONVERT(CHAR(7), calendar_date, 23)                       AS year_month,
       CAST(CASE WHEN iso_weekday = 7 THEN 1 ELSE 0 END AS BIT)  AS is_sunday
FROM numbered;
GO
