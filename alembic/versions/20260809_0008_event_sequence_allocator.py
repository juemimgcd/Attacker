"""Add the atomic per-Run event sequence allocator."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260809_0008"
down_revision: str | Sequence[str] | None = "20260730_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("event_sequence", sa.Integer(), nullable=False, server_default="0"),
    )
    op.execute(
        sa.text(
            """
            UPDATE runs
            SET event_sequence = COALESCE(
                (SELECT MAX(events.sequence) FROM events WHERE events.run_id = runs.id),
                0
            )
            """
        )
    )


def downgrade() -> None:
    op.drop_column("runs", "event_sequence")
