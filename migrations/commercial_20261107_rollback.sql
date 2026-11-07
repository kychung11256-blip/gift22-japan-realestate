-- STAGING ONLY: restore backed-up database is recommended (retains audit history).
-- Compatibility rollback removes added tables but retains nullable agent_id columns.
-- This script does not touch original listings, clients, shortlists or media bytes.
BEGIN;
DROP TABLE IF EXISTS media_assets;
DROP TABLE IF EXISTS agent_listing_assignments;
DROP TABLE IF EXISTS agent_client_assignments;
DROP TABLE IF EXISTS users;
DROP TABLE IF EXISTS audit_log;
COMMIT;
