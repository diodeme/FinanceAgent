"""Durable finance operations and reusable multi-turn artifacts.

Revision ID: 0002_finance_runtime
Revises: 0001_baseline
"""

from alembic import op

revision = "0002_finance_runtime"
down_revision = "0001_baseline"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE fin.builtin_agents (
          user_id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL UNIQUE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    for name in ("agent_id", "reply_id", "tool_call_id", "operation", "idempotency_key"):
        op.execute(f"ALTER TABLE fin.task_runs ADD COLUMN {name} TEXT")
    op.execute("""
        CREATE UNIQUE INDEX uq_fin_operation_call
        ON fin.task_runs (user_id, session_id, idempotency_key)
        WHERE idempotency_key IS NOT NULL
    """)
    op.execute("ALTER TABLE fin.task_runs DROP CONSTRAINT task_runs_phase_check")
    op.execute("""
        ALTER TABLE fin.task_runs ADD CONSTRAINT task_runs_phase_check
        CHECK (phase IN ('collecting','tabulating','analyzing','exporting','reading','done'))
    """)
    op.execute("ALTER TABLE fin.task_artifacts DROP CONSTRAINT task_artifacts_run_id_kind_key")
    op.execute("ALTER TABLE fin.task_artifacts DROP CONSTRAINT task_artifacts_kind_check")
    op.execute("""
        ALTER TABLE fin.task_artifacts ADD CONSTRAINT task_artifacts_kind_check
        CHECK (kind IN ('dataset','table','narrative','export'))
    """)
    op.execute("ALTER TABLE fin.task_artifacts ADD COLUMN title TEXT NOT NULL DEFAULT 'Financial result'")
    op.execute("ALTER TABLE fin.task_artifacts ADD COLUMN input_ids TEXT[] NOT NULL DEFAULT '{}'")
    op.execute("ALTER TABLE fin.task_artifacts ADD COLUMN filename TEXT")
    op.execute("ALTER TABLE fin.task_artifacts ADD COLUMN mime_type TEXT")
    op.execute("CREATE INDEX ix_fin_artifacts_run_created ON fin.task_artifacts (run_id, created_at DESC)")
    op.execute("ALTER TABLE fin.source_documents ALTER COLUMN local_path DROP NOT NULL")
    op.execute("ALTER TABLE fin.source_documents ALTER COLUMN fetched_at DROP NOT NULL")
    op.execute("""
        ALTER TABLE fin.source_documents ADD COLUMN fetch_status TEXT NOT NULL DEFAULT 'fetched'
        CHECK (fetch_status IN ('metadata_only','fetched','unavailable'))
    """)
    op.execute("ALTER TABLE fin.source_documents ADD COLUMN fetch_error TEXT")


def downgrade() -> None:
    raise RuntimeError(
        "Automatic downgrade of finance runtime is disabled: reusable artifacts and "
        "operation identities cannot be safely collapsed to the legacy unique-kind schema."
    )
