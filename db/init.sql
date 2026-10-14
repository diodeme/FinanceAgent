-- Runs once on first init of the postgres data volume (as superuser).
-- - vector extension for doc_chunks (pgvector image)
-- - fin schema: domain tables are versioned in finagent/migrations/ and
--   upgraded by `python -m finagent.migrate` before backend boot.
--   This file only provisions the extension/schema; it never evolves tables.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE SCHEMA IF NOT EXISTS fin;
