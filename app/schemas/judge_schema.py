from typing import Any
from pydantic import BaseModel,Field
from app.schemas.attack_sample_schema import RiskLevel


# 定义目标 Agent 返回内容、状态和调用错误信息。
class TargetResponse(BaseModel):
    status_code:int | None = None
    body:dict[str,Any] | list[Any] | None = None
    text:str = ""
    latency_ms:int = 0
    error:str | None = None


# 定义规则判断后的违规状态、风险等级和命中原因。
class JudgeResult(BaseModel):
    violated:bool
    risk_level:RiskLevel
    matched_patterns:list[str] = Field(default_factory=list)
    reason:str


# 定义一次攻击执行从请求到判断结果的完整记录。
class AttackRunResult(BaseModel):
    target_name:str
    sample_id:str
    request_body:dict[str,Any]
    target_response:TargetResponse
    judge_result:JudgeResult
