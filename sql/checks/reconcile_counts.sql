-- Live reconciliation (D-028, Q12), part 1: sizes and run times of the two databases.
-- Compares the database built over several incremental runs (nordvet) with a fresh full
-- load (nordvet_fresh). Both names are fixed here; see src/nordvet/ingest/reconcile.py.
SELECT 'appointments, incremental' AS what, CAST(COUNT(*) AS VARCHAR(30)) AS value
FROM nordvet.staging.appointment
UNION ALL
SELECT 'appointments, fresh', CAST(COUNT(*) AS VARCHAR(30))
FROM nordvet_fresh.staging.appointment
UNION ALL
SELECT 'clinics, incremental', CAST(COUNT(*) AS VARCHAR(30))
FROM nordvet.staging.clinic
UNION ALL
SELECT 'clinics, fresh', CAST(COUNT(*) AS VARCHAR(30))
FROM nordvet_fresh.staging.clinic
UNION ALL
SELECT 'API runs, incremental database', CAST(COUNT(*) AS VARCHAR(30))
FROM nordvet.raw.run
WHERE source = 'api'
UNION ALL
SELECT 'last API run started (UTC), incremental database', CONVERT(VARCHAR(19), MAX(started_at), 126)
FROM nordvet.raw.run
WHERE source = 'api'
UNION ALL
SELECT 'fresh load started (UTC)', CONVERT(VARCHAR(19), MAX(started_at), 126)
FROM nordvet_fresh.raw.run
WHERE source = 'api';
