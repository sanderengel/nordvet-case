-- Live reconciliation (D-028, Q12), part 3: clinics that differ between the two databases.
-- Clinics are a full refresh from the latest response, so there should be no difference at all.
SELECT COALESCE(i.id, f.id) AS id,
       CASE WHEN f.id IS NULL THEN 'only_incremental'
            WHEN i.id IS NULL THEN 'only_fresh'
            ELSE 'differs' END AS kind
FROM nordvet.staging.clinic AS i
FULL OUTER JOIN nordvet_fresh.staging.clinic AS f ON f.id = i.id
WHERE f.id IS NULL
   OR i.id IS NULL
   OR EXISTS (
        SELECT i.name, i.city, i.country, i.timezone, i.active, i.opened_on
        EXCEPT
        SELECT f.name, f.city, f.country, f.timezone, f.active, f.opened_on
   )
ORDER BY id;
