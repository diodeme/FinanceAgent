-- Runs once on first init of the postgres data volume (as superuser).
-- - vector extension for doc_chunks (pgvector image)
-- - fin schema: our domain tables (DDL lives in backend/finagent/schema.sql,
--   applied idempotently by `python -m finagent.migrate` on backend boot;
--   this file only bootstraps what that DDL needs)
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS fin;
