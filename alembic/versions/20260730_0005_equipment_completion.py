"""Complete immutable equipment and cleanup facts."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260730_0005"
down_revision: str | Sequence[str] | None = "20260730_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("provider_instances") as batch:
        batch.drop_constraint(
            "uq_provider_instance_revision",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_provider_instance_revision",
            [
                "instance_id",
                "provider_package_id",
                "provider_version",
                "package_checksum",
                "config_revision",
                "secret_binding_revision",
            ],
        )
    with op.batch_alter_table("equipment_packages") as batch:
        batch.add_column(sa.Column("name", sa.String(300), nullable=False, server_default=""))
        batch.add_column(
            sa.Column(
                "source_type",
                sa.String(32),
                nullable=False,
                server_default="local_directory",
            )
        )
        batch.add_column(sa.Column("source_ref", sa.Text(), nullable=False, server_default=""))
        batch.add_column(
            sa.Column(
                "signature_status",
                sa.String(32),
                nullable=False,
                server_default="not_present",
            )
        )
        batch.add_column(sa.Column("publisher_id", sa.String(200)))
        batch.add_column(sa.Column("installed_at", sa.DateTime(timezone=True)))
    with op.batch_alter_table("resource_leases") as batch:
        batch.add_column(
            sa.Column("cleanup_attempts", sa.Integer(), nullable=False, server_default="0")
        )
        batch.add_column(sa.Column("cleanup_error", sa.Text()))
    with op.batch_alter_table("run_equipment_snapshots") as batch:
        batch.add_column(sa.Column("config_json", sa.JSON()))


def downgrade() -> None:
    with op.batch_alter_table("run_equipment_snapshots") as batch:
        batch.drop_column("config_json")
    with op.batch_alter_table("resource_leases") as batch:
        batch.drop_column("cleanup_error")
        batch.drop_column("cleanup_attempts")
    with op.batch_alter_table("equipment_packages") as batch:
        batch.drop_column("installed_at")
        batch.drop_column("publisher_id")
        batch.drop_column("signature_status")
        batch.drop_column("source_ref")
        batch.drop_column("source_type")
        batch.drop_column("name")
    with op.batch_alter_table("provider_instances") as batch:
        batch.drop_constraint(
            "uq_provider_instance_revision",
            type_="unique",
        )
        batch.create_unique_constraint(
            "uq_provider_instance_revision",
            [
                "instance_id",
                "config_revision",
                "secret_binding_revision",
            ],
        )
