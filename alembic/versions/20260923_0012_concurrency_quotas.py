"""Add shared, expiring concurrency slots for orchestrated Runs."""

import sqlalchemy as sa

from alembic import op

revision = "20260923_0012"
down_revision = "20260912_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "concurrency_quotas",
        sa.Column("resource_key", sa.String(80), primary_key=True),
        sa.Column("capacity", sa.Integer(), nullable=False),
    )
    op.create_table(
        "concurrency_leases",
        sa.Column(
            "resource_key",
            sa.String(80),
            sa.ForeignKey("concurrency_quotas.resource_key", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("slot", sa.Integer(), primary_key=True),
        sa.Column("owner_token", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_concurrency_leases_owner", "concurrency_leases", ["owner_token"])


def downgrade() -> None:
    op.drop_index("ix_concurrency_leases_owner", table_name="concurrency_leases")
    op.drop_table("concurrency_leases")
    op.drop_table("concurrency_quotas")
