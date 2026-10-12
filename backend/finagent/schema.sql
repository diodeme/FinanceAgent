-- FinanceAgent domain schema (`fin`). Idempotent: safe to run on every boot.
-- Applied by `python -m finagent.migrate`. Mirrors docs/数据结构.md — keep
-- the two in sync when changing anything here.
--
-- Requires: CREATE EXTENSION vector (done in db/init.sql at volume init).
-- Note: source_documents precedes fact_financials (FK target must exist).

-- ---------------------------------------------------------------- registries

CREATE TABLE IF NOT EXISTS fin.company_registry (
  company        TEXT PRIMARY KEY,             -- ticker, e.g. 'AAPL'
  cik            TEXT NOT NULL UNIQUE,         -- SEC CIK, zero-padded to 10
  name_official  TEXT NOT NULL,
  aliases        TEXT[] NOT NULL DEFAULT '{}',
  fy_end_month   SMALLINT NOT NULL
                 CHECK (fy_end_month BETWEEN 1 AND 12),
  sector         TEXT NOT NULL,
  ir_domain      TEXT
);

CREATE TABLE IF NOT EXISTS fin.metric_registry (
  metric        TEXT PRIMARY KEY,              -- 'revenue' / 'gross_margin'
  kind          TEXT NOT NULL CHECK (kind IN ('base','derived')),
  period_type   TEXT NOT NULL
                CHECK (period_type IN ('duration','instant')),
  xbrl_concepts TEXT[],                        -- base: candidate concepts, priority order
  formula       TEXT,                          -- derived: expression over base_metrics
  base_metrics  TEXT[],                        -- derived: closure-expansion inputs
  unit          TEXT NOT NULL,                 -- 'USD' | 'USD/shares' | 'shares' | 'pure'
  is_core       BOOLEAN NOT NULL DEFAULT false,
  definition    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fin.source_registry (
  source_id    TEXT PRIMARY KEY,               -- 'sec_edgar_xbrl' | 'web_search'
  source_type  TEXT NOT NULL CHECK (source_type IN ('official_api','web')),
  endpoint     TEXT,
  applies_to   TEXT[],
  priority     SMALLINT NOT NULL,
  trust_level  TEXT NOT NULL CHECK (trust_level IN ('verified','unverified')),
  notes        TEXT
);

-- ---------------------------------------------------------------- facts

CREATE TABLE IF NOT EXISTS fin.source_documents (
  doc_id          TEXT PRIMARY KEY,
  company         TEXT NOT NULL REFERENCES fin.company_registry,
  doc_type        TEXT NOT NULL,               -- '10-Q' | '10-K' | 'press_release' | 'web_page'
  period_end      DATE,
  url             TEXT NOT NULL,
  accession       TEXT,
  fetched_at      TIMESTAMPTZ NOT NULL,
  local_path      TEXT NOT NULL,
  checksum        TEXT,
  trust_level     TEXT NOT NULL,
  kb_document_id  TEXT,                        -- AgentScope knowledge-base doc id
  extracted_at    TIMESTAMPTZ                  -- web_llm extraction done (idempotency guard)
);

CREATE TABLE IF NOT EXISTS fin.fact_financials (
  fact_id       BIGSERIAL PRIMARY KEY,
  company       TEXT NOT NULL REFERENCES fin.company_registry,
  concept       TEXT NOT NULL,                 -- raw: 'us-gaap:Revenues' | 'web:<name>'
  metric        TEXT REFERENCES fin.metric_registry,
  period_start  DATE,                          -- NULL for instant (balance-sheet) facts
  period_end    DATE NOT NULL,
  value         NUMERIC(24,4) NOT NULL,        -- source units, never rescaled
  unit          TEXT NOT NULL,
  form          TEXT,                          -- '10-Q' | '10-K' | '8-K'
  frame         TEXT,                          -- SEC calendar-period marker, e.g. 'CY2025Q2'
  source_type   TEXT NOT NULL
                CHECK (source_type IN ('xbrl_api','web_llm','manual')),
  source_ref    TEXT NOT NULL,                 -- accession number or URL
  doc_id        TEXT REFERENCES fin.source_documents,
  filed_at      DATE,                          -- restatement resolution: latest wins
  retrieved_at  TIMESTAMPTZ NOT NULL,
  confidence    TEXT NOT NULL CHECK (confidence IN ('verified','low')),
  CONSTRAINT uq_fact UNIQUE NULLS NOT DISTINCT
    (company, concept, period_start, period_end, source_ref)
);
CREATE INDEX IF NOT EXISTS ix_fact_lookup
  ON fin.fact_financials (company, metric, period_end);

CREATE TABLE IF NOT EXISTS fin.doc_chunks (
  chunk_id     TEXT PRIMARY KEY,
  collection   TEXT NOT NULL,
  document_id  TEXT NOT NULL,
  chunk        JSONB NOT NULL,
  metadata     JSONB NOT NULL,
  embedding    vector(1536) NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_doc_chunks_vec
  ON fin.doc_chunks USING hnsw (embedding vector_cosine_ops);
CREATE INDEX IF NOT EXISTS ix_doc_chunks_doc
  ON fin.doc_chunks (collection, document_id);

-- ---------------------------------------------------------------- tasks

CREATE TABLE IF NOT EXISTS fin.task_runs (
  run_id        TEXT PRIMARY KEY,
  session_id    TEXT NOT NULL,                 -- orchestrator's AgentScope session
  user_id       TEXT NOT NULL,
  user_query    TEXT NOT NULL,
  status        TEXT NOT NULL
                CHECK (status IN ('running','succeeded','partial','failed','interrupted')),
  phase         TEXT NOT NULL
                CHECK (phase IN ('collecting','tabulating','analyzing','done')),
  data_request  JSONB,                         -- DataRequest snapshot; resume without memory
  error         TEXT,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  finished_at   TIMESTAMPTZ,
  heartbeat_at  TIMESTAMPTZ                    -- crash detection: scanner marks interrupted
);
CREATE INDEX IF NOT EXISTS ix_task_runs_user
  ON fin.task_runs (user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS fin.run_coverage (
  run_id      TEXT NOT NULL REFERENCES fin.task_runs,
  company     TEXT NOT NULL REFERENCES fin.company_registry,
  period_end  DATE NOT NULL,
  form        TEXT NOT NULL,
  accession   TEXT,                            -- NULL when missing
  status      TEXT NOT NULL
              CHECK (status IN ('covered','low_confidence','missing')),
  attempts    SMALLINT NOT NULL DEFAULT 0,     -- bounded retry, survives crashes
  reason      TEXT,
  PRIMARY KEY (run_id, company, period_end, form)
);

CREATE TABLE IF NOT EXISTS fin.run_steps (
  step_id     BIGSERIAL PRIMARY KEY,
  run_id      TEXT REFERENCES fin.task_runs,   -- NULL: ad-hoc UI chats (no task yet)
  seq         BIGINT NOT NULL,                 -- global order; per-run 1..n assigned by writer
  actor       TEXT NOT NULL,                   -- agent id (later: orchestrator/data/analysis)
  session_id  TEXT NOT NULL,
  msg_id      TEXT,                            -- Msg/ChatResponse/tool-call id → agentscope messages
  step_type   TEXT NOT NULL CHECK (step_type IN ('llm','tool','compress')),
  name        TEXT NOT NULL,                   -- model or tool name
  tokens_in   INT,
  tokens_out  INT,
  cost_usd    NUMERIC(10,6),                   -- tokens × price table (TODO: price config)
  latency_ms  INT,
  detail      JSONB,
  status      TEXT NOT NULL CHECK (status IN ('ok','error')),
  created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ix_run_steps_run ON fin.run_steps (run_id, seq);

CREATE TABLE IF NOT EXISTS fin.task_artifacts (
  artifact_id  TEXT PRIMARY KEY,
  run_id       TEXT NOT NULL REFERENCES fin.task_runs,
  kind         TEXT NOT NULL CHECK (kind IN ('table','narrative')),
  format       TEXT NOT NULL,                  -- 'xlsx' | 'csv' | 'md' | 'text'
  path         TEXT,
  content      TEXT,
  meta         JSONB NOT NULL DEFAULT '{}',    -- cells[] / citations[] (see docs)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (run_id, kind)                        -- re-running a task reuses, not duplicates
);

-- ---------------------------------------------------------------- memory

CREATE TABLE IF NOT EXISTS fin.agent_memory (
  memory_id      TEXT PRIMARY KEY,
  user_id        TEXT NOT NULL,
  kind           TEXT NOT NULL
                 CHECK (kind IN ('preference','watchlist','correction')),
  key            TEXT NOT NULL,                -- 'preferred_metrics' | 'last_companies' | ...
  value          JSONB NOT NULL,
  source_run_id  TEXT REFERENCES fin.task_runs,
  updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (user_id, kind, key)
);
