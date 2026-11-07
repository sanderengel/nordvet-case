-- Does text survive: Python parameter -> server -> OPENJSON -> back. The one parameter is a JSON object with key v.

SELECT j.v FROM OPENJSON(?) WITH (v NVARCHAR(100) N'$.v') AS j;
