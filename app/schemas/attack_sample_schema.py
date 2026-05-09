from enum import Enum
from pydantic import BaseModel,Field


# 定义攻击样本的风险等级枚举。
class RiskLevel(str,Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"



# 定义单条攻击样本的结构化字段。
class AttackSample(BaseModel):
    id:str
    name:str
    category:str
    severity:RiskLevel = RiskLevel.medium
    role:str = "user"
    prompt:str
    expected_violation:str
    judge_patterns:list[str] = Field(default_factory=list)
