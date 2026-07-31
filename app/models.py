"""Attacker SQL 业务事实模型；唯一约束承载幂等、顺序和绑定不变量。"""

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    """所有 SQLAlchemy 业务模型的声明式基类。"""


# Run 输入快照与顶层状态：回答“这次评测在什么条件下执行”。
class TargetRecord(Base):
    __tablename__ = "targets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class DatasetSnapshotRecord(Base):
    __tablename__ = "dataset_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    snapshot_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class PolicySnapshotRecord(Base):
    __tablename__ = "policy_snapshots"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EvaluationRunRecord(Base):
    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    target_id: Mapped[str] = mapped_column(ForeignKey("targets.id"), nullable=False)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_snapshots.id"), nullable=False)
    policy_id: Mapped[str] = mapped_column(ForeignKey("policy_snapshots.id"), nullable=False)
    baseline_run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"))
    thread_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    mode: Mapped[str] = mapped_column(String(32), default="deterministic")
    status: Mapped[str] = mapped_column(String(32), default="running")
    total_cases: Mapped[int] = mapped_column(Integer, default=0)
    completed_cases: Mapped[int] = mapped_column(Integer, default=0)
    target_call_count: Mapped[int] = mapped_column(Integer, default=0)
    violation_count: Mapped[int] = mapped_column(Integer, default=0)
    refused_count: Mapped[int] = mapped_column(Integer, default=0)
    safe_count: Mapped[int] = mapped_column(Integer, default=0)
    error_count: Mapped[int] = mapped_column(Integer, default=0)
    budget_aborted_count: Mapped[int] = mapped_column(Integer, default=0)
    false_positive_count: Mapped[int] = mapped_column(Integer, default=0)
    defense_overblock_count: Mapped[int] = mapped_column(Integer, default=0)
    planner_call_count: Mapped[int] = mapped_column(Integer, default=0)
    planner_token_count: Mapped[int] = mapped_column(Integer, default=0)
    tool_call_count: Mapped[int] = mapped_column(Integer, default=0)
    policy_denied_count: Mapped[int] = mapped_column(Integer, default=0)
    approval_required_count: Mapped[int] = mapped_column(Integer, default=0)
    terminal_reason: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


# 执行证据与结论：operation_id 和 run 内 sequence 防止重复或乱序落证。
class RunStepRecord(Base):
    __tablename__ = "steps"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_steps_run_sequence"),
        UniqueConstraint("operation_id", name="uq_steps_operation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(300), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EventRecord(Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("run_id", "sequence", name="uq_events_run_sequence"),
        UniqueConstraint("operation_id", name="uq_events_operation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class FindingRecord(Base):
    __tablename__ = "findings"
    __table_args__ = (UniqueConstraint("operation_id", name="uq_findings_operation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    step_id: Mapped[str] = mapped_column(ForeignKey("steps.id"), nullable=False)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id"), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    category: Mapped[str] = mapped_column(String(100), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    fingerprint: Mapped[str | None] = mapped_column(String(64), index=True)
    evidence_event_ids: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    is_control: Mapped[bool] = mapped_column(Boolean, default=False)
    score: Mapped[float] = mapped_column(Float, default=1.0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    __table_args__ = (
        UniqueConstraint("run_id", "case_id", name="uq_approvals_run_case"),
        UniqueConstraint("operation_id", name="uq_approvals_operation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    risk_summary: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_by: Mapped[str | None] = mapped_column(String(200))
    reason: Mapped[str | None] = mapped_column(Text)


# 带状态评测事实：夹具、快照和检索事件共同证明跨请求行为。
class StateFixtureRecord(Base):
    __tablename__ = "state_fixtures"
    __table_args__ = (UniqueConstraint("operation_id", name="uq_state_fixtures_operation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    scope_id: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    namespace: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(200), nullable=False)
    content_summary: Mapped[str] = mapped_column(Text, nullable=False)
    provenance: Mapped[str] = mapped_column(String(200), nullable=False)
    permissions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    poisoned: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class StateSnapshotRecord(Base):
    __tablename__ = "state_snapshots"
    __table_args__ = (UniqueConstraint("operation_id", name="uq_state_snapshots_operation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"))
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    phase: Mapped[str] = mapped_column(String(32), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    items_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class RetrievalEventRecord(Base):
    __tablename__ = "retrieval_events"
    __table_args__ = (UniqueConstraint("operation_id", name="uq_retrieval_events_operation_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"))
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    case_id: Mapped[str] = mapped_column(String(200), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(100), nullable=False)
    user_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(100), nullable=False)
    query_summary: Mapped[str] = mapped_column(Text, nullable=False)
    documents_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    permission_filter_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class ReplayRecord(Base):
    __tablename__ = "replays"
    __table_args__ = (UniqueConstraint("replay_run_id", name="uq_replays_replay_run_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    replay_run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    diff_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# 装备供应链与执行事实：包、实例、冻结绑定、执行、租约和审计相互独立。
class EquipmentPackageRecord(Base):
    __tablename__ = "equipment_packages"
    __table_args__ = (
        UniqueConstraint(
            "package_type",
            "package_id",
            "version",
            name="uq_equipment_packages_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    package_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    package_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(300), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    source_path: Mapped[str] = mapped_column(Text, nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="local_directory")
    source_ref: Mapped[str] = mapped_column(Text, nullable=False, default="")
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    trust_level: Mapped[str] = mapped_column(String(32), nullable=False)
    signature_status: Mapped[str] = mapped_column(String(32), nullable=False, default="not_present")
    publisher_id: Mapped[str | None] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    validation_errors_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    installed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    loaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderInstanceRecord(Base):
    __tablename__ = "provider_instances"
    __table_args__ = (
        UniqueConstraint(
            "instance_id",
            "provider_package_id",
            "provider_version",
            "package_checksum",
            "config_revision",
            "secret_binding_revision",
            name="uq_provider_instance_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    instance_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    provider_package_id: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    provider_version: Mapped[str] = mapped_column(String(100), nullable=False)
    package_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    environment: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    config_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    secret_binding_revision: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_refs_json: Mapped[dict[str, str]] = mapped_column(JSON, nullable=False)
    allowed_hosts_json: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    health_status: Mapped[str] = mapped_column(String(32), default="unknown", nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )


class RunEquipmentSnapshotRecord(Base):
    __tablename__ = "run_equipment_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "package_type",
            "package_id",
            "binding_key",
            name="uq_run_equipment_binding",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), nullable=False, index=True)
    package_type: Mapped[str] = mapped_column(String(32), nullable=False)
    package_id: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[str] = mapped_column(String(100), nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    manifest_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    provider_instance_id: Mapped[str | None] = mapped_column(String(200))
    binding_key: Mapped[str] = mapped_column(String(200), nullable=False)
    config_revision: Mapped[str | None] = mapped_column(String(64))
    config_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    secret_binding_revision: Mapped[str | None] = mapped_column(String(64))
    capability_contract_id: Mapped[str | None] = mapped_column(String(200))
    capability_contract_checksum: Mapped[str | None] = mapped_column(String(64))
    test_principal_ref: Mapped[str | None] = mapped_column(String(500))
    target_binding_ref: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class EquipmentExecutionRecord(Base):
    __tablename__ = "equipment_executions"
    __table_args__ = (
        UniqueConstraint("operation_id", name="uq_equipment_executions_operation_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    step_id: Mapped[str | None] = mapped_column(ForeignKey("steps.id"))
    operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    package_id: Mapped[str] = mapped_column(String(200), nullable=False)
    package_version: Mapped[str] = mapped_column(String(100), nullable=False)
    package_checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    provider_instance_id: Mapped[str | None] = mapped_column(String(200))
    config_revision: Mapped[str | None] = mapped_column(String(64))
    secret_binding_revision: Mapped[str | None] = mapped_column(String(64))
    capability: Mapped[str | None] = mapped_column(String(200))
    capability_contract_checksum: Mapped[str | None] = mapped_column(String(64))
    test_principal_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    physical_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    input_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    output_summary_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    evidence_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResourceLeaseRecord(Base):
    __tablename__ = "resource_leases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id"), index=True)
    created_by_operation_id: Mapped[str] = mapped_column(String(340), nullable=False)
    provider_instance_id: Mapped[str] = mapped_column(String(200), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    external_resource_id: Mapped[str] = mapped_column(String(500), nullable=False)
    cleanup_contract: Mapped[str] = mapped_column(String(200), nullable=False)
    cleanup_payload_ref: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    last_cleanup_operation_id: Mapped[str | None] = mapped_column(String(340))
    cleanup_attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    cleanup_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    cleaned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EquipmentAuditEventRecord(Base):
    __tablename__ = "equipment_audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    operation_id: Mapped[str] = mapped_column(String(340), unique=True, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    package_id: Mapped[str | None] = mapped_column(String(200), index=True)
    evidence_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


# 持久任务事实：租约 owner/token/expiry 共同决定 Worker 是否仍有写入权。
class RunJobRecord(Base):
    __tablename__ = "run_jobs"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_run_jobs_request_id"),
        Index("ix_run_jobs_claim", "status", "available_at", "priority"),
        Index("ix_run_jobs_lease_expiry", "status", "lease_expires_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    request_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    payload_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_summary: Mapped[str | None] = mapped_column(Text)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, onupdate=utc_now
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkerHeartbeatRecord(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    draining: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    active_jobs: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
