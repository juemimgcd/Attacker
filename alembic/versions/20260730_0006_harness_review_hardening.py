"""Harden equipment execution and immutable Run bindings."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260730_0006"
down_revision: str | Sequence[str] | None = "20260730_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("equipment_executions") as batch:
        batch.add_column(
            sa.Column(
                "request_fingerprint",
                sa.String(64),
                nullable=True,
            )
        )
        batch.add_column(sa.Column("secret_binding_revision", sa.String(64)))
        batch.add_column(
            sa.Column(
                "result_json",
                sa.JSON(),
                nullable=True,
            )
        )
    with op.batch_alter_table("run_equipment_snapshots") as batch:
        batch.drop_constraint("uq_run_equipment_binding", type_="unique")
        batch.add_column(
            sa.Column(
                "binding_key",
                sa.String(200),
                nullable=False,
                server_default="",
            )
        )
    op.execute(
        """
        UPDATE run_equipment_snapshots
        SET binding_key = COALESCE(provider_instance_id, '')
        """
    )
    with op.batch_alter_table("run_equipment_snapshots") as batch:
        batch.create_unique_constraint(
            "uq_run_equipment_binding",
            ["run_id", "package_type", "package_id", "binding_key"],
        )


def downgrade() -> None:
    with op.batch_alter_table("run_equipment_snapshots") as batch:
        batch.drop_constraint("uq_run_equipment_binding", type_="unique")
        batch.drop_column("binding_key")
        batch.create_unique_constraint(
            "uq_run_equipment_binding",
            ["run_id", "package_type", "package_id", "provider_instance_id"],
        )
    with op.batch_alter_table("equipment_executions") as batch:
        batch.drop_column("result_json")
        batch.drop_column("secret_binding_revision")
        batch.drop_column("request_fingerprint")
