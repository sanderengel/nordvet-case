-- What the last staging rebuild changed compared with the snapshot (005_snapshot_before.sql).
-- One row per kind: new ids; status changes (from, to); rows newly soft-deleted; ids that
-- disappeared, which should never happen because raw is append-only.
SELECT 'new' AS kind, CAST(NULL AS VARCHAR(9)) AS from_status, CAST(NULL AS VARCHAR(9)) AS to_status,
       COUNT(*) AS n
FROM staging.appointment AS a
WHERE NOT EXISTS (SELECT 1 FROM #before AS b WHERE b.id = a.id)
UNION ALL
SELECT 'status', b.status, a.status, COUNT(*)
FROM staging.appointment AS a
JOIN #before AS b ON b.id = a.id
WHERE a.status <> b.status
GROUP BY b.status, a.status
UNION ALL
SELECT 'deleted', NULL, NULL, COUNT(*)
FROM staging.appointment AS a
JOIN #before AS b ON b.id = a.id
WHERE a.deleted_at IS NOT NULL AND b.deleted_at IS NULL
UNION ALL
SELECT 'gone', NULL, NULL, COUNT(*)
FROM #before AS b
WHERE NOT EXISTS (SELECT 1 FROM staging.appointment AS a WHERE a.id = b.id);
