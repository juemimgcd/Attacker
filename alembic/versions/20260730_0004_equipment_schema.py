"""Add Harness equipment catalog and execution facts."""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260730_0004"
down_revision: str | Sequence[str] | None = "20260728_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "equipment_packages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("package_type", sa.String(32), nullable=False),
        sa.Column("package_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("source_path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("trust_level", sa.String(32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("validation_errors_json", sa.JSON(), nullable=False),
        sa.Column("discovered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("loaded_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint(
            "package_type",
            "package_id",
            "version",
            name="uq_equipment_packages_identity",
        ),
    )
    op.create_index("ix_equipment_packages_package_type", "equipment_packages", ["package_type"])
    op.create_index("ix_equipment_packages_package_id", "equipment_packages", ["package_id"])

    op.create_table(
        "provider_instances",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("instance_id", sa.String(200), nullable=False),
        sa.Column("provider_package_id", sa.String(200), nullable=False),
        sa.Column("provider_version", sa.String(100), nullable=False),
        sa.Column("package_checksum", sa.String(64), nullable=False),
        sa.Column("display_name", sa.String(200), nullable=False),
        sa.Column("environment", sa.String(100), nullable=False),
        sa.Column("config_revision", sa.String(64), nullable=False),
        sa.Column("config_json", sa.JSON(), nullable=False),
        sa.Column("secret_binding_revision", sa.String(64), nullable=False),
        sa.Column("secret_refs_json", sa.JSON(), nullable=False),
        sa.Column("allowed_hosts_json", sa.JSON(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("health_status", sa.String(32), nullable=False, server_default="unknown"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "instance_id",
            "config_revision",
            "secret_binding_revision",
            name="uq_provider_instance_revision",
        ),
    )
    op.create_index("ix_provider_instances_instance_id", "provider_instances", ["instance_id"])
    op.create_index(
        "ix_provider_instances_provider_package_id",
        "provider_instances",
        ["provider_package_id"],
    )
    op.create_index("ix_provider_instances_environment", "provider_instances", ["environment"])

    op.create_table(
        "run_equipment_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("package_type", sa.String(32), nullable=False),
        sa.Column("package_id", sa.String(200), nullable=False),
        sa.Column("version", sa.String(100), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("manifest_json", sa.JSON(), nullable=False),
        sa.Column("provider_instance_id", sa.String(200)),
        sa.Column("config_revision", sa.String(64)),
        sa.Column("secret_binding_revision", sa.String(64)),
        sa.Column("capability_contract_id", sa.String(200)),
        sa.Column("capability_contract_checksum", sa.String(64)),
        sa.Column("test_principal_ref", sa.String(500)),
        sa.Column("target_binding_ref", sa.String(500)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint(
            "run_id",
            "package_type",
            "package_id",
            "provider_instance_id",
            name="uq_run_equipment_binding",
        ),
    )
    op.create_index("ix_run_equipment_snapshots_run_id", "run_equipment_snapshots", ["run_id"])

    op.create_table(
        "equipment_executions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id")),
        sa.Column("step_id", sa.String(36), sa.ForeignKey("steps.id")),
        sa.Column("operation_id", sa.String(340), nullable=False),
        sa.Column("package_id", sa.String(200), nullable=False),
        sa.Column("package_version", sa.String(100), nullable=False),
        sa.Column("package_checksum", sa.String(64), nullable=False),
        sa.Column("provider_instance_id", sa.String(200)),
        sa.Column("config_revision", sa.String(64)),
        sa.Column("capability", sa.String(200)),
        sa.Column("capability_contract_checksum", sa.String(64)),
        sa.Column("test_principal_ref", sa.String(500), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("physical_attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("input_summary_json", sa.JSON(), nullable=False),
        sa.Column("output_summary_json", sa.JSON(), nullable=False),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("error_code", sa.String(100)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("operation_id", name="uq_equipment_executions_operation_id"),
    )
    op.create_index("ix_equipment_executions_run_id", "equipment_executions", ["run_id"])

    op.create_table(
        "resource_leases",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("runs.id")),
        sa.Column("created_by_operation_id", sa.String(340), nullable=False),
        sa.Column("provider_instance_id", sa.String(200), nullable=False),
        sa.Column("resource_type", sa.String(100), nullable=False),
        sa.Column("external_resource_id", sa.String(500), nullable=False),
        sa.Column("cleanup_contract", sa.String(200), nullable=False),
        sa.Column("cleanup_payload_ref", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("last_cleanup_operation_id", sa.String(340)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cleaned_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_resource_leases_run_id", "resource_leases", ["run_id"])

    op.create_table(
        "equipment_audit_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("operation_id", sa.String(340), nullable=False, unique=True),
        sa.Column("event_type", sa.String(80), nullable=False),
        sa.Column("package_id", sa.String(200)),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(
        "ix_equipment_audit_events_event_type",
        "equipment_audit_events",
        ["event_type"],
    )
    op.create_index(
        "ix_equipment_audit_events_package_id",
        "equipment_audit_events",
        ["package_id"],
    )


def downgrade() -> None:
    op.drop_table("equipment_audit_events")
    op.drop_table("resource_leases")
    op.drop_table("equipment_executions")
    op.drop_table("run_equipment_snapshots")
    op.drop_table("provider_instances")
    op.drop_table("equipment_packages")
