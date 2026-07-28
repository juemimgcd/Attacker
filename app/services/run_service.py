import hashlib
import ipaddress
import json
from pathlib import Path
from time import perf_counter
from typing import Any

from app.repositories.run_repository import RunRepository
from app.schemas.attack_sample_schema import (
    AttackSample,
    BlackBoxCase,
    CaseKind,
    EvaluatorRules,
    EvaluatorType,
    RiskLevel,
)
from app.schemas.judge_schema import TargetResponse
from app.schemas.run_schema import (
    CaseRunResult,
    DeterministicRunRequest,
    EvaluationOutcome,
    EvaluationResult,
    LoadedDataset,
    RunBudget,
    TargetCallEvidence,
)
from app.schemas.target_schema import TargetConfig
from app.services.evaluator_service import EvaluatorService
from app.services.sample_loader import BlackBoxDatasetLoader
from app.services.target_connector.http_connector import HTTPTargetConnector


class DeterministicRunService:
    def __init__(
        self,
        repository: RunRepository,
        *,
        dataset_loader: BlackBoxDatasetLoader | None = None,
        connector: HTTPTargetConnector | None = None,
        evaluator: EvaluatorService | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_loader = dataset_loader or BlackBoxDatasetLoader()
        self.connector = connector or HTTPTargetConnector()
        self.evaluator = evaluator or EvaluatorService()

    async def run(self, request: DeterministicRunRequest) -> dict[str, Any]:
        self._validate_target(request)
        dataset_path = Path(request.dataset_path).resolve()
        samples_root = Path("samples").resolve()
        if not dataset_path.is_relative_to(samples_root):
            raise ValueError("dataset_path must resolve inside the samples directory")
        dataset = await self.dataset_loader.load(dataset_path, request.case_ids)
        return await self._run_dataset(request, dataset)

    async def run_single(
        self,
        *,
        target: TargetConfig,
        sample: AttackSample,
    ) -> dict[str, Any]:
        evaluator_type = (
            EvaluatorType.system_prompt_leakage
            if "system" in sample.category
            else EvaluatorType.direct_prompt_injection
        )
        case = BlackBoxCase(
            id=sample.id,
            name=sample.name,
            category=sample.category,
            severity=sample.severity,
            kind=CaseKind.attack,
            prompts=[sample.prompt],
            expected_violation=sample.expected_violation,
            expected_outcome="refused",
            evaluator=EvaluatorRules(
                type=evaluator_type,
                violation_patterns=sample.judge_patterns,
            ),
        )
        snapshot = {
            "name": "api_single_case",
            "cases": [{"name": case.id, "inputs": case.model_dump(mode="json")}],
        }
        snapshot_bytes = json.dumps(snapshot, sort_keys=True).encode()
        dataset = LoadedDataset(
            name="api_single_case",
            version="1",
            source_path=Path("<api>"),
            sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
            cases=[case],
            snapshot=snapshot,
        )
        request = DeterministicRunRequest(
            target=target,
            budget=RunBudget(max_cases=1, max_target_calls=1),
        )
        self._validate_target(request)
        return await self._run_dataset(request, dataset)

    async def _run_dataset(
        self,
        request: DeterministicRunRequest,
        dataset: LoadedDataset,
    ) -> dict[str, Any]:
        secret_values = self._secret_values(request.target)
        target_snapshot = self._redact(
            request.target.model_dump(mode="json"),
            {"authorization", "token", "api_key", "secret", "secret_key"},
            secret_values,
        )
        run_id = await self.repository.create_run(
            target_snapshot=target_snapshot,
            dataset=dataset,
            budget=request.budget,
        )

        started = perf_counter()
        target_call_count = 0
        counts = {
            "violation": 0,
            "refused": 0,
            "safe": 0,
            "error": 0,
            "budget_aborted": 0,
            "false_positive": 0,
            "defense_overblock": 0,
        }

        for step_sequence, case in enumerate(dataset.cases, start=1):
            operation_id = f"{run_id}:{case.id}"
            existing = await self.repository.get_case_result(operation_id)
            if existing is not None:
                self._increment_counts(counts, case, EvaluationOutcome(existing["outcome"]))
                target_call_count += len(existing.get("calls", []))
                continue

            budget_reason = self._budget_reason(
                request.budget,
                started=started,
                case_index=step_sequence,
                target_call_count=target_call_count,
                required_calls=len(case.prompts) * case.repeat_count,
            )
            if budget_reason:
                result = self._budget_aborted_result(case, request.budget, budget_reason)
            else:
                result = await self._execute_case(
                    request,
                    case,
                    started=started,
                    calls_before_case=target_call_count,
                )
                target_call_count += len(result.calls)

            await self.repository.record_case_result(
                run_id=run_id,
                operation_id=operation_id,
                step_sequence=step_sequence,
                result=result,
            )
            self._increment_counts(counts, case, result.outcome)

        await self.repository.finalize_run(
            run_id,
            counts=counts,
            target_call_count=target_call_count,
        )
        return await self.repository.get_report_rows(run_id)

    @staticmethod
    def _validate_target(request: DeterministicRunRequest) -> None:
        target = request.target
        if target.allow_public_target:
            return
        host = target.endpoint.host
        if host is None:
            raise ValueError("target endpoint must include a host")
        if host == "localhost":
            return
        try:
            address = ipaddress.ip_address(host)
            if address.is_private or address.is_loopback:
                return
        except ValueError:
            pass
        raise ValueError(
            "public or unresolved targets require allow_public_target=true and explicit authorization"
        )

    async def _execute_case(
        self,
        request: DeterministicRunRequest,
        case: BlackBoxCase,
        *,
        started: float,
        calls_before_case: int,
    ) -> CaseRunResult:
        calls: list[TargetCallEvidence] = []
        raw_responses: list[TargetResponse] = []
        redact_fields = set(case.redact_fields)
        secret_values = self._secret_values(request.target)

        for _ in range(case.repeat_count):
            messages: list[dict[str, str]] = []
            for prompt in case.prompts:
                mid_case_reason = self._budget_reason(
                    request.budget,
                    started=started,
                    case_index=1,
                    target_call_count=calls_before_case + len(calls),
                    required_calls=1,
                    ignore_case_limit=True,
                )
                if mid_case_reason:
                    return CaseRunResult(
                        case=case,
                        outcome=EvaluationOutcome.budget_aborted,
                        calls=calls,
                        evaluation=self._budget_evaluation(case, mid_case_reason),
                        budget=request.budget,
                    )

                messages.append({"role": "user", "content": prompt})
                request_body, response = await self.connector.call(
                    target=request.target,
                    prompt=prompt,
                    messages=messages,
                )
                raw_responses.append(response)
                calls.append(
                    TargetCallEvidence(
                        request_body=self._redact(
                            request_body,
                            redact_fields,
                            secret_values,
                        ),
                        response=TargetResponse.model_validate(
                            self._redact(
                                response.model_dump(mode="json"),
                                redact_fields,
                                secret_values,
                            )
                        ),
                    )
                )
                messages.append({"role": "assistant", "content": response.text})

        evaluation = self.evaluator.evaluate(
            case,
            raw_responses,
            max_response_bytes=request.budget.max_response_bytes,
        )
        return CaseRunResult(
            case=case,
            outcome=evaluation.outcome,
            calls=calls,
            evaluation=evaluation,
            budget=request.budget,
        )

    @staticmethod
    def _budget_reason(
        budget: RunBudget,
        *,
        started: float,
        case_index: int,
        target_call_count: int,
        required_calls: int,
        ignore_case_limit: bool = False,
    ) -> str | None:
        if not ignore_case_limit and case_index > budget.max_cases:
            return f"case budget of {budget.max_cases} exhausted"
        if perf_counter() - started >= budget.max_duration_seconds:
            return f"duration budget of {budget.max_duration_seconds}s exhausted"
        if target_call_count + required_calls > budget.max_target_calls:
            return f"target call budget of {budget.max_target_calls} exhausted"
        return None

    @classmethod
    def _budget_aborted_result(
        cls,
        case: BlackBoxCase,
        budget: RunBudget,
        reason: str,
    ) -> CaseRunResult:
        return CaseRunResult(
            case=case,
            outcome=EvaluationOutcome.budget_aborted,
            calls=[],
            evaluation=cls._budget_evaluation(case, reason),
            budget=budget,
        )

    @staticmethod
    def _budget_evaluation(case: BlackBoxCase, reason: str) -> EvaluationResult:
        return EvaluationResult(
            outcome=EvaluationOutcome.budget_aborted,
            violated=False,
            risk_level=RiskLevel.low,
            reason=reason,
            evaluator_type=case.evaluator.type.value,
        )

    @staticmethod
    def _increment_counts(
        counts: dict[str, int],
        case: BlackBoxCase,
        outcome: EvaluationOutcome,
    ) -> None:
        counts[outcome.value] += 1
        if case.kind == CaseKind.control and outcome == EvaluationOutcome.violation:
            counts["false_positive"] += 1
        if case.kind == CaseKind.control and outcome == EvaluationOutcome.refused:
            counts["defense_overblock"] += 1

    @classmethod
    def _redact(
        cls,
        value: Any,
        redacted_keys: set[str],
        secret_values: set[str],
    ) -> Any:
        if isinstance(value, dict):
            return {
                key: (
                    "[REDACTED]"
                    if cls._is_redacted_key(key, redacted_keys)
                    else cls._redact(item, redacted_keys, secret_values)
                )
                for key, item in value.items()
            }
        if isinstance(value, list):
            return [cls._redact(item, redacted_keys, secret_values) for item in value]
        if isinstance(value, str):
            for secret in secret_values:
                value = value.replace(secret, "[REDACTED]")
        return value

    @staticmethod
    def _is_redacted_key(key: str, redacted_keys: set[str]) -> bool:
        normalized = key.lower().replace("-", "_")
        return any(
            normalized == candidate or normalized.endswith(f"_{candidate}")
            for candidate in redacted_keys
        )

    @classmethod
    def _secret_values(cls, target: TargetConfig) -> set[str]:
        secrets = {target.auth.token} if target.auth.token else set()
        secrets.update(
            value
            for key, value in target.headers.items()
            if cls._is_redacted_key(
                key,
                {"authorization", "token", "api_key", "secret", "secret_key"},
            )
        )
        return {secret for secret in secrets if secret}
