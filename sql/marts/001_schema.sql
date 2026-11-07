-- Marts layer: views on top of staging; nothing is stored here.
-- Run in file order (001, 010, ...): each view only uses views from lower-numbered files.

IF SCHEMA_ID(N'marts') IS NULL EXEC (N'CREATE SCHEMA marts');
GO
