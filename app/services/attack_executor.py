"""单条黑盒样本的传输与 Judge 编排；正式批量 Run 由 RunService 持久化。"""

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.schemas.attack_sample_schema import AttackSample
from app.schemas.judge_schema import AttackRunResult, TargetResponse
from app.schemas.target_schema import TargetConfig
from app.services.judge_engine import JudgeEngine, judge_engine
from app.services.target_connector.http_connector import (
    HTTPTargetConnector,
    http_target_connector,
)

TargetCallObserver = Callable[[dict[str, Any], TargetResponse], None]


class AttackExecutor:
    """调用 Target 后交给 Judge，返回不含持久化副作用的单次结果。"""

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
        *,
        on_target_call: TargetCallObserver | None = None,
    ) -> AttackRunResult:
        evidence_id = str(uuid4())
        request_body, target_response = await self.connector.call(
            target=target,
            prompt=sample.prompt,
        )
        if on_target_call is not None:
            on_target_call(request_body, target_response)
        judge_result = self.judge.judge(
            sample=sample,
            target_response=target_response,
            evidence_refs=[evidence_id],
            refusal_status_codes=target.refusal_status_codes,
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
