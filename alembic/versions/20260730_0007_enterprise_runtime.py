"""Add durable run jobs and worker heartbeats."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260730_0007"
down_revision: str | Sequence[str] | None = "20260730_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "run_jobs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("request_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("payload_fingerprint", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(200)),
        sa.Column("lease_token", sa.String(64)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("result_json", sa.JSON()),
        sa.Column("error_code", sa.String(100)),
        sa.Column("error_summary", sa.Text()),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("request_id", name="uq_run_jobs_request_id"),
    )
    op.create_index(
        "ix_run_jobs_claim",
        "run_jobs",
        ["status", "available_at", "priority"],
    )
    op.create_index(
        "ix_run_jobs_lease_expiry",
        "run_jobs",
        ["status", "lease_expires_at"],
    )
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(200), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("draining", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("active_jobs", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_run_jobs_lease_expiry", table_name="run_jobs")
    op.drop_index("ix_run_jobs_claim", table_name="run_jobs")
    op.drop_table("run_jobs")
