from uuid import uuid4

from app.schemas.attack_sample_schema import AttackSample
from app.schemas.judge_schema import AttackRunResult
from app.schemas.target_schema import TargetConfig
from app.services.judge_engine import JudgeEngine, judge_engine
from app.services.target_connector.http_connector import (
    HTTPTargetConnector,
    http_target_connector,
)


# 编排一次攻击样本对目标 Agent 的执行和规则判断。
class AttackExecutor:
    def __init__(
        self,
        *,
        connector: HTTPTargetConnector = http_target_connector,
        judge: JudgeEngine = judge_engine,
    ) -> None:
        self.connector = connector
        self.judge = judge

    # 执行单条攻击样本并返回完整运行结果。
    async def run_once(
        self,
        target: TargetConfig,
        sample: AttackSample,
    ) -> AttackRunResult:
        evidence_id = str(uuid4())
        request_body, target_response = await self.connector.call(
            target=target,
            prompt=sample.prompt,
        )
        judge_result = self.judge.judge(
            sample=sample,
            target_response=target_response,
            evidence_refs=[evidence_id],
        )
        return AttackRunResult(
            evidence_id=evidence_id,
            target_name=target.name,
            sample_id=sample.id,
            request_body=request_body,
            target_response=target_response,
            judge_result=judge_result,
        )


attack_executor = AttackExecutor()
