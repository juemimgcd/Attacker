"""Add a cross-process lease for adaptive Run resume operations."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260831_0010"
down_revision: str | Sequence[str] | None = "20260809_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("runs") as batch_op:
        batch_op.add_column(sa.Column("resume_claim_kind", sa.String(32)))
        batch_op.add_column(sa.Column("resume_claim_checkpoint_id", sa.String(200)))
        batch_op.add_column(sa.Column("resume_claim_owner_token", sa.String(64)))
        batch_op.add_column(sa.Column("resume_claim_expires_at", sa.DateTime(timezone=True)))
    op.execute(
        sa.text(
            """
            UPDATE provider_instances
            SET enabled = false
            WHERE enabled = true
              AND id IN (
                SELECT id
                FROM (
                    SELECT id,
                           row_number() OVER (
                               PARTITION BY instance_id
                               ORDER BY updated_at DESC, created_at DESC, id DESC
                           ) AS enabled_rank
                    FROM provider_instances
                    WHERE enabled = true
                ) AS ranked
                WHERE enabled_rank > 1
              )
            """
        )
    )
    op.create_index(
        "uq_provider_instances_enabled_instance",
        "provider_instances",
        ["instance_id"],
        unique=True,
        postgresql_where=sa.text("enabled IS TRUE"),
        sqlite_where=sa.text("enabled = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_provider_instances_enabled_instance",
        table_name="provider_instances",
    )
    with op.batch_alter_table("runs") as batch_op:
        batch_op.drop_column("resume_claim_expires_at")
        batch_op.drop_column("resume_claim_owner_token")
        batch_op.drop_column("resume_claim_checkpoint_id")
        batch_op.drop_column("resume_claim_kind")
