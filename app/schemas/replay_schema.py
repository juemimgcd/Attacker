from pydantic import BaseModel

from app.schemas.stateful_schema import StatefulProfile
from app.schemas.target_schema import TargetConfig


class ReplayRunRequest(BaseModel):
    profile: StatefulProfile | None = None
    target: TargetConfig | None = None
