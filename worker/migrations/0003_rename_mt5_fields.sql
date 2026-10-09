-- 0003_rename_mt5_fields.sql — drop MT5 naming from the signals table.
--
-- Apply with: wrangler d1 execute forex-signals-db --file=./migrations/0003_rename_mt5_fields.sql
-- Non-destructive: renames the column in place, existing data untouched.
-- (Fresh installs should just apply schema.sql, which already uses the
-- new names.)
--
-- NOTE: D1/SQLite has no "RENAME COLUMN IF NOT EXISTS". If this migration
-- was already applied once, applying it again errors on the missing
-- source column — safe to retry, just confirm broker_result exists.

ALTER TABLE signals RENAME COLUMN mt5_result TO broker_result;
