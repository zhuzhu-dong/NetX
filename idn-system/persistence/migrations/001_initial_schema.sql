-- 001_initial_schema: placeholder for existing schema migration.
-- The IntentStore already handles DDL inline; this migration
-- ensures the _migrations tracking table survives alongside
-- the application tables.
-- Existing tables (sessions, intents, messages, intent_jobs, events,
-- watcher_*, experience_cases) are managed by IntentStore.__init__().
-- This file serves as the baseline for future schema additions.
SELECT 1;  -- no-op: marks version 001 as applied
