"""Freeze the original 11-table fin schema.

Revision ID: 0001_baseline
Revises: None

This snapshot is immutable. Later schema changes belong in new revisions.
Each statement is executed separately for SQLAlchemy's asyncpg driver.
"""

from alembic import op

revision = "0001_baseline"
down_revision = None
branch_labels = None
depends_on = None


DDL = (
    """
    CREATE TABLE fin.company_registry (
      company        TEXT PRIMARY KEY,
      cik            TEXT NOT NULL UNIQUE,
      name_official  TEXT NOT NULL,
      aliases        TEXT[] NOT NULL DEFAULT '{}',
      fy_end_month   SMALLINT NOT NULL
                     CHECK (fy_end_month BETWEEN 1 AND 12),
      sector         TEXT NOT NULL,
      ir_domain      TEXT
    )
    """,
    """
    CREATE TABLE fin.metric_registry (
      metric        TEXT PRIMARY KEY,
      kind          TEXT NOT NULL CHECK (kind IN ('base','derived')),
      period_type   TEXT NOT NULL
                    CHECK (period_type IN ('duration','instant')),
      xbrl_concepts TEXT[],
      formula       TEXT,
      base_metrics  TEXT[],
      unit          TEXT NOT NULL,
      is_core       BOOLEAN NOT NULL DEFAULT false,
      definition    TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE fin.source_registry (
      source_id    TEXT PRIMARY KEY,
      source_type  TEXT NOT NULL CHECK (source_type IN ('official_api','web')),
      endpoint     TEXT,
      applies_to   TEXT[],
      priority     SMALLINT NOT NULL,
      trust_level  TEXT NOT NULL CHECK (trust_level IN ('verified','unverified')),
      notes        TEXT
    )
    """,
    """
    CREATE TABLE fin.source_documents (
      doc_id          TEXT PRIMARY KEY,
      company         TEXT NOT NULL REFERENCES fin.company_registry,
      doc_type        TEXT NOT NULL,
      period_end      DATE,
      url             TEXT NOT NULL,
      accession       TEXT,
      fetched_at      TIMESTAMPTZ NOT NULL,
      local_path      TEXT NOT NULL,
      checksum        TEXT,
      trust_level     TEXT NOT NULL,
      kb_document_id  TEXT,
      extracted_at    TIMESTAMPTZ
    )
    """,
    """
    CREATE TABLE fin.fact_financials (
      fact_id       BIGSERIAL PRIMARY KEY,
      company       TEXT NOT NULL REFERENCES fin.company_registry,
      concept       TEXT NOT NULL,
      metric        TEXT REFERENCES fin.metric_registry,
      period_start  DATE,
      period_end    DATE NOT NULL,
      value         NUMERIC(24,4) NOT NULL,
      unit          TEXT NOT NULL,
      form          TEXT,
      frame         TEXT,
      source_type   TEXT NOT NULL
                    CHECK (source_type IN ('xbrl_api','web_llm','manual')),
      source_ref    TEXT NOT NULL,
      doc_id        TEXT REFERENCES fin.source_documents,
      filed_at      DATE,
      retrieved_at  TIMESTAMPTZ NOT NULL,
      confidence    TEXT NOT NULL CHECK (confidence IN ('verified','low')),
      CONSTRAINT uq_fact UNIQUE NULLS NOT DISTINCT
        (company, concept, period_start, period_end, source_ref)
    )
    """,
    "CREATE INDEX ix_fact_lookup ON fin.fact_financials (company, metric, period_end)",
    """
    CREATE TABLE fin.doc_chunks (
      chunk_id     TEXT PRIMARY KEY,
      collection   TEXT NOT NULL,
      document_id  TEXT NOT NULL,
      chunk        JSONB NOT NULL,
      metadata     JSONB NOT NULL,
      embedding    vector(1536) NOT NULL
    )
    """,
    "CREATE INDEX ix_doc_chunks_vec ON fin.doc_chunks USING hnsw (embedding vector_cosine_ops)",
    "CREATE INDEX ix_doc_chunks_doc ON fin.doc_chunks (collection, document_id)",
    """
    CREATE TABLE fin.task_runs (
      run_id        TEXT PRIMARY KEY,
      session_id    TEXT NOT NULL,
      user_id       TEXT NOT NULL,
      user_query    TEXT NOT NULL,
      status        TEXT NOT NULL
                    CHECK (status IN ('running','succeeded','partial','failed','interrupted')),
      phase         TEXT NOT NULL
                    CHECK (phase IN ('collecting','tabulating','analyzing','done')),
      data_request  JSONB,
      error         TEXT,
      created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
      finished_at   TIMESTAMPTZ,
      heartbeat_at  TIMESTAMPTZ
    )
    """,
    "CREATE INDEX ix_task_runs_user ON fin.task_runs (user_id, created_at DESC)",
    """
    CREATE TABLE fin.run_coverage (
      run_id      TEXT NOT NULL REFERENCES fin.task_runs,
      company     TEXT NOT NULL REFERENCES fin.company_registry,
      period_end  DATE NOT NULL,
      form        TEXT NOT NULL,
      accession   TEXT,
      status      TEXT NOT NULL
                  CHECK (status IN ('covered','low_confidence','missing')),
      attempts    SMALLINT NOT NULL DEFAULT 0,
      reason      TEXT,
      PRIMARY KEY (run_id, company, period_end, form)
    )
    """,
    """
    CREATE TABLE fin.run_steps (
      step_id     BIGSERIAL PRIMARY KEY,
      run_id      TEXT REFERENCES fin.task_runs,
      seq         BIGINT NOT NULL,
      actor       TEXT NOT NULL,
      session_id  TEXT NOT NULL,
      msg_id      TEXT,
      step_type   TEXT NOT NULL CHECK (step_type IN ('llm','tool','compress')),
      name        TEXT NOT NULL,
      tokens_in   INT,
      tokens_out  INT,
      cost_usd    NUMERIC(10,6),
      latency_ms  INT,
      detail      JSONB,
      status      TEXT NOT NULL CHECK (status IN ('ok','error')),
      created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX ix_run_steps_run ON fin.run_steps (run_id, seq)",
    """
    CREATE TABLE fin.task_artifacts (
      artifact_id  TEXT PRIMARY KEY,
      run_id       TEXT NOT NULL REFERENCES fin.task_runs,
      kind         TEXT NOT NULL CHECK (kind IN ('table','narrative')),
      format       TEXT NOT NULL,
      path         TEXT,
      content      TEXT,
      meta         JSONB NOT NULL DEFAULT '{}',
      created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (run_id, kind)
    )
    """,
    """
    CREATE TABLE fin.agent_memory (
      memory_id      TEXT PRIMARY KEY,
      user_id        TEXT NOT NULL,
      kind           TEXT NOT NULL
                     CHECK (kind IN ('preference','watchlist','correction')),
      key            TEXT NOT NULL,
      value          JSONB NOT NULL,
      source_run_id  TEXT REFERENCES fin.task_runs,
      updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
      UNIQUE (user_id, kind, key)
    )
    """,
)


def upgrade() -> None:
    for statement in DDL:
        op.execute(statement)


def downgrade() -> None:
    raise RuntimeError(
        "Downgrade below 0001_baseline is not supported: it would delete all fin data."
    )
