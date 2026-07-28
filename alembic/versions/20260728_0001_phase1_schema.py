"""Create phase-one black-box evaluation tables."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "targets",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("endpoint", sa.Text(), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "dataset_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("snapshot_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "policy_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(36), sa.ForeignKey("targets.id"), nullable=False),
        sa.Column(
            "dataset_id", sa.String(36), sa.ForeignKey("dataset_snapshots.id"), nullable=False
        ),
        sa.Column("policy_id", sa.String(36), sa.ForeignKey("policy_snapshots.id"), nullable=False),
        sa.Column("mode", sa.String(32), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("total_cases", sa.Integer(), nullable=False),
        sa.Column("completed_cases", sa.Integer(), nullable=False),
        sa.Column("target_call_count", sa.Integer(), nullable=False),
        sa.Column("violation_count", sa.Integer(), nullable=False),
        sa.Column("refused_count", sa.Integer(), nullable=False),
        sa.Column("safe_count", sa.Integer(), nullable=False),
        sa.Column("error_count", sa.Integer(), nullable=False),
        sa.Column("budget_aborted_count", sa.Integer(), nullable=False),
        sa.Column("false_positive_count", sa.Integer(), nullable=False),
        sa.Column("defense_overblock_count", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "steps",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("operation_id", sa.String(300), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("result_json", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_steps_run_sequence"),
        sa.UniqueConstraint("operation_id", name="uq_steps_operation_id"),
    )
    op.create_index("ix_steps_run_id", "steps", ["run_id"])
    op.create_table(
        "events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("steps.id")),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
        sa.UniqueConstraint("operation_id", name="uq_events_operation_id"),
    )
    op.create_index("ix_events_run_id", "events", ["run_id"])
    op.create_table(
        "findings",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("steps.id"), nullable=False),
        sa.Column("event_id", sa.String(36), sa.ForeignKey("events.id"), nullable=False),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("category", sa.String(100), nullable=False),
        sa.Column("risk_level", sa.String(32), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("evidence_event_ids", sa.JSON(), nullable=False),
        sa.Column("is_control", sa.Boolean(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id", name="uq_findings_operation_id"),
    )
    op.create_index("ix_findings_run_id", "findings", ["run_id"])


def downgrade() -> None:
    op.drop_index("ix_findings_run_id", table_name="findings")
    op.drop_table("findings")
    op.drop_index("ix_events_run_id", table_name="events")
    op.drop_table("events")
    op.drop_index("ix_steps_run_id", table_name="steps")
    op.drop_table("steps")
    op.drop_table("runs")
    op.drop_table("policy_snapshots")
    op.drop_table("dataset_snapshots")
    op.drop_table("targets")
