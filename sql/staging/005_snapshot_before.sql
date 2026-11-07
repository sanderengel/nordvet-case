-- Remember the current staging state, so the loader can report what a run changed.
-- Lives in a temporary table for this connection only; nothing is kept in the database.
IF OBJECT_ID(N'tempdb..#before') IS NOT NULL DROP TABLE #before;
GO
SELECT id, status, deleted_at INTO #before FROM staging.appointment;
