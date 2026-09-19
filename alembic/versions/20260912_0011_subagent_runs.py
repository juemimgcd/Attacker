"""Persist coordinator manifests without copying child credentials or evidence."""

import sqlalchemy as sa

from alembic import op

revision = "20260912_0011"
down_revision = "20260831_0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "subagent_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("dispatch_finished", sa.Boolean(), nullable=False),
        sa.Column("errors_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("subagent_runs")
