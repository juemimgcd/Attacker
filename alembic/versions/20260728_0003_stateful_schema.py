"""Add stateful evaluation and replay tables."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260728_0003"
down_revision: str | Sequence[str] | None = "20260728_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("findings") as batch:
        batch.add_column(sa.Column("fingerprint", sa.String(64)))
        batch.create_index("ix_findings_fingerprint", ["fingerprint"])

    op.create_table(
        "state_fixtures",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("scope_id", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("namespace", sa.String(100), nullable=False),
        sa.Column("resource_id", sa.String(200), nullable=False),
        sa.Column("content_summary", sa.Text(), nullable=False),
        sa.Column("provenance", sa.String(200), nullable=False),
        sa.Column("permissions_json", sa.JSON(), nullable=False),
        sa.Column("poisoned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("operation_id", name="uq_state_fixtures_operation_id"),
    )
    op.create_index("ix_state_fixtures_run_id", "state_fixtures", ["run_id"])
    op.create_index("ix_state_fixtures_scope_id", "state_fixtures", ["scope_id"])

    op.create_table(
        "state_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("steps.id")),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("items_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id", name="uq_state_snapshots_operation_id"),
    )
    op.create_index("ix_state_snapshots_run_id", "state_snapshots", ["run_id"])

    op.create_table(
        "retrieval_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("steps.id")),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("case_id", sa.String(200), nullable=False),
        sa.Column("tenant_id", sa.String(100), nullable=False),
        sa.Column("user_id", sa.String(100), nullable=False),
        sa.Column("session_id", sa.String(100), nullable=False),
        sa.Column("query_summary", sa.Text(), nullable=False),
        sa.Column("documents_json", sa.JSON(), nullable=False),
        sa.Column("permission_filter_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("operation_id", name="uq_retrieval_events_operation_id"),
    )
    op.create_index("ix_retrieval_events_run_id", "retrieval_events", ["run_id"])

    op.create_table(
        "replays",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("source_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("replay_run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("diff_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("replay_run_id", name="uq_replays_replay_run_id"),
    )
    op.create_index("ix_replays_source_run_id", "replays", ["source_run_id"])


def downgrade() -> None:
    op.drop_index("ix_replays_source_run_id", table_name="replays")
    op.drop_table("replays")
    op.drop_index("ix_retrieval_events_run_id", table_name="retrieval_events")
    op.drop_table("retrieval_events")
    op.drop_index("ix_state_snapshots_run_id", table_name="state_snapshots")
    op.drop_table("state_snapshots")
    op.drop_index("ix_state_fixtures_scope_id", table_name="state_fixtures")
    op.drop_index("ix_state_fixtures_run_id", table_name="state_fixtures")
    op.drop_table("state_fixtures")
    with op.batch_alter_table("findings") as batch:
        batch.drop_index("ix_findings_fingerprint")
        batch.drop_column("fingerprint")
