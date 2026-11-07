-- Every staging.clinic row as plain text, for comparing with the source JSON in Python.

SELECT id, name, city, country, timezone, CAST(active AS INT), CONVERT(VARCHAR(10), opened_on, 23)
FROM staging.clinic;
