"""Replay 请求模型；凭据按运行时重新提供，不从历史快照恢复明文。"""

from pydantic import BaseModel, Field

from app.schemas.equipment_schema import EquipmentReplayMode
from app.schemas.stateful_schema import StatefulProfile
from app.schemas.target_schema import TargetConfig


class ReplayRunRequest(BaseModel):
    mode: EquipmentReplayMode = EquipmentReplayMode.same_binding_rerun
    evaluator_version: str = "core-evidence-v1"
    profile: StatefulProfile | None = None
    target: TargetConfig | None = None
    preauthorize_approvals: bool = False
    equipment_bindings: dict[str, dict] = Field(default_factory=dict)
