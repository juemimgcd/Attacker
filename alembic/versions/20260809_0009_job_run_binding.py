"""Bind a durable Job to its Run as soon as the Run is created."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0009"
down_revision: str | Sequence[str] | None = "20260809_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch_op:
        batch_op.add_column(sa.Column("run_id", sa.String(36), nullable=True))
        batch_op.create_foreign_key(
            "fk_run_jobs_run_id_runs",
            "runs",
            ["run_id"],
            ["id"],
        )
        batch_op.create_index("ix_run_jobs_run_id", ["run_id"])


def downgrade() -> None:
    with op.batch_alter_table("run_jobs") as batch_op:
        batch_op.drop_index("ix_run_jobs_run_id")
        batch_op.drop_constraint("fk_run_jobs_run_id_runs", type_="foreignkey")
        batch_op.drop_column("run_id")
