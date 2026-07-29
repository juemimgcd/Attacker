"""Add gray-box workflow and approval fields."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0002"
down_revision: str | Sequence[str] | None = "20260728_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch:
        batch.add_column(sa.Column("baseline_run_id", sa.String(36)))
        batch.add_column(sa.Column("thread_id", sa.String(100)))
        batch.add_column(
            sa.Column("planner_call_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("planner_token_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("tool_call_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("policy_denied_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("approval_required_count", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("terminal_reason", sa.Text()))
        batch.create_foreign_key(
            "fk_runs_baseline_run_id",
            "runs",
            ["baseline_run_id"],
            ["id"],
        )
        batch.create_unique_constraint("uq_runs_thread_id", ["thread_id"])

    op.create_table(
        "approvals",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("risk_summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("resolved_by", sa.String(200)),
        sa.Column("reason", sa.Text()),
        sa.UniqueConstraint("run_id", "case_id", name="uq_approvals_run_case"),
        sa.UniqueConstraint("operation_id", name="uq_approvals_operation_id"),
    )
    op.create_index("ix_approvals_run_id", "approvals", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_approvals_run_id", table_name="approvals")
    op.drop_table("approvals")
    with op.batch_alter_table("runs") as batch:
        batch.drop_constraint("uq_runs_thread_id", type_="unique")
        batch.drop_constraint("fk_runs_baseline_run_id", type_="foreignkey")
        batch.drop_column("terminal_reason")
        batch.drop_column("approval_required_count")
        batch.drop_column("policy_denied_count")
        batch.drop_column("tool_call_count")
        batch.drop_column("planner_token_count")
        batch.drop_column("planner_call_count")
        batch.drop_column("thread_id")
        batch.drop_column("baseline_run_id")
