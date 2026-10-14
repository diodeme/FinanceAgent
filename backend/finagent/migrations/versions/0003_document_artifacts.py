"""Allow immutable original-filing artifacts using existing path/metadata storage.

Revision ID: 0003_document_artifacts
Revises: 0002_finance_runtime
"""

from alembic import op

revision = "0003_document_artifacts"
down_revision = "0002_finance_runtime"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE fin.task_artifacts DROP CONSTRAINT task_artifacts_kind_check")
    op.execute("""
        ALTER TABLE fin.task_artifacts ADD CONSTRAINT task_artifacts_kind_check
        CHECK (kind IN ('dataset','table','narrative','export','document'))
    """)


def downgrade() -> None:
    # Never delete saved document references just to fit the older constraint.
    if op.get_bind().exec_driver_sql(
        "SELECT EXISTS (SELECT 1 FROM fin.task_artifacts WHERE kind = 'document')"
    ).scalar_one():
        raise RuntimeError("Cannot downgrade while document artifacts exist; no data was removed.")
    op.execute("ALTER TABLE fin.task_artifacts DROP CONSTRAINT task_artifacts_kind_check")
    op.execute("""
        ALTER TABLE fin.task_artifacts ADD CONSTRAINT task_artifacts_kind_check
        CHECK (kind IN ('dataset','table','narrative','export'))
    """)
