"""确定性黑盒 Run 的数据集冻结、执行、Evidence 持久化和脱敏编排。"""

from __future__ import annotations

import hashlib
import ipaddress
import json
from asyncio import CancelledError
from collections.abc import Callable
from pathlib import Path
from time import perf_counter
from typing import TYPE_CHECKING, Any, Literal

from loguru import logger

from app.repositories.run_repository import RunRepository
from app.schemas.attack_sample_schema import (
    AttackSample,
    BlackBoxCase,
    CaseKind,
    EvaluatorRules,
    EvaluatorType,
    RiskLevel,
)
from app.schemas.judge_schema import (
    AttackRunResult,
    EvaluationIssue,
    EvaluationVerdict,
    TargetResponse,
)
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
from app.services.attack_executor import AttackExecutor
from app.services.evaluator_service import EvaluatorService
from app.services.prompt_governance import redact_sensitive_text
from app.services.sample_loader import BlackBoxDatasetLoader
from app.services.target_binding import canonical_target_binding, canonical_target_ref
from app.services.target_connector.http_connector import HTTPTargetConnector

if TYPE_CHECKING:
    from app.services.equipment_service import EquipmentService

_CORE_REDACTED_KEYS = {
    "authorization",
    "token",
    "api_key",
    "secret",
    "secret_key",
    "password",
}


class DeterministicRunService:
    """固定 Case 顺序和平台规则；不承诺外部模型输出逐字确定。"""

    def __init__(
        self,
        repository: RunRepository,
        *,
        dataset_loader: BlackBoxDatasetLoader | None = None,
        connector: HTTPTargetConnector | None = None,
        evaluator: EvaluatorService | None = None,
        attack_executor: AttackExecutor | None = None,
        equipment_service: EquipmentService | None = None,
    ) -> None:
        self.repository = repository
        self.dataset_loader = dataset_loader or BlackBoxDatasetLoader()
        self.connector = connector or HTTPTargetConnector()
        self.evaluator = evaluator or EvaluatorService()
        self.attack_executor = attack_executor or AttackExecutor(connector=self.connector)
        self.equipment_service = equipment_service

    async def run(self, request: DeterministicRunRequest) -> dict[str, Any]:
        self._validate_target(request)
        dataset_path = Path(request.dataset_path).resolve()
        samples_root = Path("samples").resolve()
        if not dataset_path.is_relative_to(samples_root):
            raise ValueError("dataset_path must resolve inside the samples directory")
        dataset = await self.dataset_loader.load(dataset_path, request.case_ids)
        return await self.run_dataset(
            target=request.target,
            dataset=dataset,
            budget=request.budget,
            mode="deterministic",
            fixture_evidence_refs=request.fixture_evidence_refs,
        )

    async def run_single(
        self,
        *,
        target: TargetConfig,
        sample: AttackSample,
    ) -> dict[str, Any]:
        dataset = self._single_case_dataset(sample)
        request = DeterministicRunRequest(
            target=target,
            budget=RunBudget(max_cases=1, max_target_calls=1),
        )
        self._validate_target(request)
        return await self.run_dataset(
            target=target,
            dataset=dataset,
            budget=request.budget,
            mode="deterministic",
        )

    async def run_layered_single(
        self,
        *,
        target: TargetConfig,
        sample: AttackSample,
    ) -> dict[str, Any]:
        dataset = self._single_case_dataset(sample)
        budget = RunBudget(max_cases=1, max_target_calls=1)
        request = DeterministicRunRequest(target=target, budget=budget)
        self._validate_target(request)
        secret_values = self._secret_values(target)
        redacted_keys = set(_CORE_REDACTED_KEYS)
        target_snapshot = self._redact_target_snapshot(
            target,
            redacted_keys,
            secret_values,
        )
        persisted_dataset = self._redact_single_case_dataset(
            dataset,
            redacted_keys,
            secret_values,
        )
        run_id = await self.repository.create_run(
            target_snapshot=target_snapshot,
            dataset=persisted_dataset,
            budget=budget,
            mode="deterministic",
        )
        counts = self._empty_counts()
        target_call_count = 0
        partial_calls: list[TargetCallEvidence] = []
        current_operation_id = f"{run_id}:freeze_equipment"

        def observe_target_call(
            request_body: dict[str, Any],
            response: TargetResponse,
        ) -> None:
            nonlocal target_call_count
            partial_calls.append(
                TargetCallEvidence(
                    request_body=self._redact(request_body, redacted_keys, secret_values),
                    response=self._redact_target_response(
                        response,
                        redacted_keys,
                        secret_values,
                    ),
                )
            )
            target_call_count += 1

        try:
            await self._freeze_equipment(run_id, target)
            current_operation_id = f"{run_id}:{sample.id}:execute"
            raw_result = await self.attack_executor.run_once(
                target=target,
                sample=sample,
                on_target_call=observe_target_call,
            )
            result = self._redact_attack_result(
                raw_result,
                redacted_keys,
                secret_values,
            )
            outcome = self._layered_outcome(result)
            current_operation_id = f"{run_id}:{sample.id}:persist"
            await self.repository.record_layered_result(
                run_id=run_id,
                operation_id=f"{run_id}:{sample.id}",
                case=persisted_dataset.cases[0],
                result=result,
                outcome=outcome,
            )
            counts[outcome.value] = 1
            partial_calls = []
            current_operation_id = f"{run_id}:finalize"
            await self.repository.finalize_run(
                run_id,
                counts=counts,
                target_call_count=target_call_count,
            )
        except CancelledError as exc:
            await self._mark_run_interrupted(
                run_id,
                status="cancelled",
                counts=counts,
                target_call_count=target_call_count,
                operation_id=current_operation_id,
                exc=exc,
                partial_case_id=sample.id,
                partial_calls=partial_calls,
            )
            raise
        except Exception as exc:
            await self._mark_run_interrupted(
                run_id,
                status="failed",
                counts=counts,
                target_call_count=target_call_count,
                operation_id=current_operation_id,
                exc=exc,
                partial_case_id=sample.id,
                partial_calls=partial_calls,
            )
            raise
        return await self.repository.get_report_rows(run_id)

    @staticmethod
    def _single_case_dataset(sample: AttackSample) -> LoadedDataset:
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
        return dataset

    async def run_dataset(
        self,
        *,
        target: TargetConfig,
        dataset: LoadedDataset,
        budget: RunBudget,
        mode: str,
        equipment_source_run_id: str | None = None,
        equipment_overrides: dict[str, dict[str, Any]] | None = None,
        fixture_evidence_refs: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """执行冻结数据集，并在每个 Case 后持久化可恢复审计事实。"""

        request = DeterministicRunRequest(
            target=target,
            budget=budget,
            fixture_evidence_refs=fixture_evidence_refs or {},
        )
        self._validate_target(request)
        fixture_case_ids = {
            case.id for case in dataset.cases if case.delivery_mode == "target_fixture"
        }
        unknown_fixture_refs = set(request.fixture_evidence_refs) - fixture_case_ids
        if unknown_fixture_refs:
            raise ValueError(
                "fixture evidence references unknown or non-fixture cases: "
                + ", ".join(sorted(unknown_fixture_refs))
            )
        secret_values = self._secret_values(request.target)
        target_snapshot = self._redact_target_snapshot(
            request.target,
            set(_CORE_REDACTED_KEYS),
            secret_values,
        )
        run_id = await self.repository.create_run(
            target_snapshot=target_snapshot,
            dataset=dataset,
            budget=request.budget,
            mode=mode,
        )
        started = perf_counter()
        target_call_count = 0
        counts = self._empty_counts()
        partial_case_id: str | None = None
        partial_calls: list[TargetCallEvidence] = []
        current_operation_id = f"{run_id}:freeze_equipment"

        def observe_target_call(call: TargetCallEvidence) -> None:
            nonlocal target_call_count
            partial_calls.append(call)
            target_call_count += 1

        try:
            await self._freeze_equipment(
                run_id,
                target,
                source_run_id=equipment_source_run_id,
                overrides=equipment_overrides,
            )
            for step_sequence, case in enumerate(dataset.cases, start=1):
                operation_id = f"{run_id}:{case.id}"
                current_operation_id = operation_id
                partial_case_id = case.id
                partial_calls = []
                existing = await self.repository.get_case_result(operation_id)
                if existing is not None:
                    self._increment_counts(
                        counts,
                        case,
                        EvaluationOutcome(existing["outcome"]),
                    )
                    target_call_count += len(existing.get("calls", []))
                    continue

                precondition_reason = self._precondition_reason(request, case)
                budget_reason = self._budget_reason(
                    request.budget,
                    started=started,
                    case_index=step_sequence,
                    target_call_count=target_call_count,
                    required_calls=len(case.prompts) * case.repeat_count,
                )
                if budget_reason:
                    result = self._budget_aborted_result(case, request.budget, budget_reason)
                elif precondition_reason:
                    result = self._not_evaluable_result(
                        case,
                        request.budget,
                        precondition_reason,
                        evidence_ref=request.fixture_evidence_refs.get(case.id),
                    )
                else:
                    result = await self._execute_case(
                        request,
                        case,
                        started=started,
                        calls_before_case=target_call_count,
                        on_target_call=observe_target_call,
                    )

                await self.repository.record_case_result(
                    run_id=run_id,
                    operation_id=operation_id,
                    step_sequence=step_sequence,
                    result=result,
                )
                self._increment_counts(counts, case, result.outcome)
                partial_case_id = None
                partial_calls = []

            current_operation_id = f"{run_id}:finalize"
            await self.repository.finalize_run(
                run_id,
                counts=counts,
                target_call_count=target_call_count,
            )
        except CancelledError as exc:
            await self._mark_run_interrupted(
                run_id,
                status="cancelled",
                counts=counts,
                target_call_count=target_call_count,
                operation_id=current_operation_id,
                exc=exc,
                partial_case_id=partial_case_id,
                partial_calls=partial_calls,
            )
            raise
        except Exception as exc:
            await self._mark_run_interrupted(
                run_id,
                status="failed",
                counts=counts,
                target_call_count=target_call_count,
                operation_id=current_operation_id,
                exc=exc,
                partial_case_id=partial_case_id,
                partial_calls=partial_calls,
            )
            raise
        return await self.repository.get_report_rows(run_id)

    async def _freeze_equipment(
        self,
        run_id: str,
        target: TargetConfig,
        *,
        source_run_id: str | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        if self.equipment_service is None:
            return
        if source_run_id is not None:
            await self.equipment_service.clone_run_bindings(
                source_run_id=source_run_id,
                target_run_id=run_id,
            )
            return
        await self.equipment_service.freeze_run_bindings(
            run_id=run_id,
            stage="blackbox",
            target_binding_ref=canonical_target_ref(target),
            test_principal_ref="core:blackbox",
            overrides=overrides,
        )

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
        on_target_call: Callable[[TargetCallEvidence], None] | None = None,
    ) -> CaseRunResult:
        """执行单个 Case，先应用预算，再保存最小充分 Evidence。"""

        calls: list[TargetCallEvidence] = []
        raw_responses: list[TargetResponse] = []
        redact_fields = set(case.redact_fields) | _CORE_REDACTED_KEYS
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
                call_evidence = TargetCallEvidence(
                    request_body=self._redact(
                        request_body,
                        redact_fields,
                        secret_values,
                    ),
                    response=self._redact_target_response(
                        response,
                        redact_fields,
                        secret_values,
                    ),
                )
                calls.append(call_evidence)
                if on_target_call is not None:
                    on_target_call(call_evidence)
                messages.append({"role": "assistant", "content": response.text})

        evaluation = self.evaluator.evaluate(
            case,
            raw_responses,
            max_response_bytes=request.budget.max_response_bytes,
            refusal_status_codes=request.target.refusal_status_codes,
        )
        return CaseRunResult(
            case=case,
            outcome=evaluation.outcome,
            calls=calls,
            evaluation=evaluation,
            budget=request.budget,
            precondition_evidence=(
                {
                    "fixture_preparation": self._redact(
                        request.fixture_evidence_refs[case.id],
                        redact_fields,
                        secret_values,
                    )
                }
                if case.id in request.fixture_evidence_refs
                else {}
            ),
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

    def _precondition_reason(
        self,
        request: DeterministicRunRequest,
        case: BlackBoxCase,
    ) -> str | None:
        if case.delivery_mode == "multi_turn" and not self.connector.supports_message_history(
            request.target
        ):
            return "target request template does not support multi-turn message history"
        if case.delivery_mode == "target_fixture" and case.id not in request.fixture_evidence_refs:
            return "target fixture preparation evidence was not provided"
        return None

    @staticmethod
    def _not_evaluable_result(
        case: BlackBoxCase,
        budget: RunBudget,
        reason: str,
        *,
        evidence_ref: str | None,
    ) -> CaseRunResult:
        evidence = {"fixture_preparation": evidence_ref} if evidence_ref else {}
        return CaseRunResult(
            case=case,
            outcome=EvaluationOutcome.not_evaluable,
            calls=[],
            evaluation=EvaluationResult(
                outcome=EvaluationOutcome.not_evaluable,
                violated=False,
                risk_level=RiskLevel.low,
                reason=reason,
                evaluator_type=case.evaluator.type.value,
                evidence_complete=False,
            ),
            budget=budget,
            precondition_evidence=evidence,
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

    @staticmethod
    def _layered_outcome(result: AttackRunResult) -> EvaluationOutcome:
        if result.judge_result.verdict == EvaluationVerdict.violation:
            return EvaluationOutcome.violation
        if result.judge_result.verdict == EvaluationVerdict.safe:
            return EvaluationOutcome.safe
        if EvaluationIssue.refusal in result.judge_result.issues:
            return EvaluationOutcome.refused
        return EvaluationOutcome.error

    @staticmethod
    def _empty_counts() -> dict[str, int]:
        return {
            "violation": 0,
            "refused": 0,
            "safe": 0,
            "error": 0,
            "budget_aborted": 0,
            "not_evaluable": 0,
            "false_positive": 0,
            "defense_overblock": 0,
        }

    async def _mark_run_interrupted(
        self,
        run_id: str,
        *,
        status: Literal["failed", "cancelled"],
        counts: dict[str, int],
        target_call_count: int,
        operation_id: str,
        exc: BaseException,
        partial_case_id: str | None,
        partial_calls: list[TargetCallEvidence],
    ) -> None:
        reason_code = type(exc).__name__[:100]
        try:
            await self.repository.mark_run_interrupted(
                run_id,
                status=status,
                counts=counts,
                target_call_count=target_call_count,
                reason_code=reason_code,
                last_operation_id=operation_id,
                partial_case_id=partial_case_id if partial_calls else None,
                partial_calls=[call.model_dump(mode="json") for call in partial_calls],
            )
        except Exception as terminal_exc:  # noqa: BLE001 - preserve the original failure
            logger.error(
                "failed to persist deterministic run terminal state",
                run_id=run_id,
                requested_status=status,
                error_type=type(terminal_exc).__name__,
            )

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
            return redact_sensitive_text(value, secret_values)
        return value

    @staticmethod
    def _redact_target_snapshot(
        target: TargetConfig,
        redacted_keys: set[str],
        secret_values: set[str],
    ) -> dict[str, Any]:
        snapshot = canonical_target_binding(target)
        if snapshot is None:
            raise ValueError("target binding is required")
        return snapshot

    @classmethod
    def _redact_target_response(
        cls,
        response: TargetResponse,
        redacted_keys: set[str],
        secret_values: set[str],
    ) -> TargetResponse:
        response_data = response.model_dump(mode="json")
        response_data["body"] = cls._redact(
            response_data["body"],
            redacted_keys,
            secret_values,
        )
        response_data["text"] = cls._redact(
            response_data["text"],
            redacted_keys,
            secret_values,
        )
        response_data["error"] = cls._redact(
            response_data["error"],
            redacted_keys,
            secret_values,
        )
        return TargetResponse.model_validate(response_data)

    @classmethod
    def _redact_attack_result(
        cls,
        result: AttackRunResult,
        redacted_keys: set[str],
        secret_values: set[str],
    ) -> AttackRunResult:
        result_data = result.model_dump(mode="json")
        result_data["target_name"] = cls._redact(
            result_data["target_name"],
            redacted_keys,
            secret_values,
        )
        result_data["request_body"] = cls._redact(
            result_data["request_body"],
            redacted_keys,
            secret_values,
        )
        result_data["target_response"] = cls._redact_target_response(
            result.target_response,
            redacted_keys,
            secret_values,
        ).model_dump(mode="json")
        judge_result = result_data["judge_result"]
        judge_result["reason"] = cls._redact(
            judge_result["reason"],
            redacted_keys,
            secret_values,
        )
        judge_result["matched_patterns"] = cls._redact(
            judge_result["matched_patterns"],
            redacted_keys,
            secret_values,
        )
        for evaluator_result in judge_result["evaluator_results"]:
            evaluator_result["reason"] = cls._redact(
                evaluator_result["reason"],
                redacted_keys,
                secret_values,
            )
            evaluator_result["matched_patterns"] = cls._redact(
                evaluator_result["matched_patterns"],
                redacted_keys,
                secret_values,
            )
        return AttackRunResult.model_validate(result_data)

    @classmethod
    def _redact_single_case_dataset(
        cls,
        dataset: LoadedDataset,
        redacted_keys: set[str],
        secret_values: set[str],
    ) -> LoadedDataset:
        redacted_cases: list[BlackBoxCase] = []
        for case in dataset.cases:
            case_data = case.model_dump(mode="json")
            for field in (
                "name",
                "prompts",
                "expected_violation",
                "attack_variant",
                "setup_requirements",
                "cleanup_steps",
            ):
                case_data[field] = cls._redact(
                    case_data[field],
                    redacted_keys,
                    secret_values,
                )
            evaluator = case_data["evaluator"]
            for field in (
                "violation_patterns",
                "exact_match_patterns",
                "line_match_patterns",
                "refusal_patterns",
                "canary",
            ):
                evaluator[field] = cls._redact(
                    evaluator[field],
                    redacted_keys,
                    secret_values,
                )
            redacted_cases.append(BlackBoxCase.model_validate(case_data))

        snapshot = {
            "name": dataset.name,
            "cases": [
                {
                    "name": case.id,
                    "inputs": case.model_dump(mode="json"),
                }
                for case in redacted_cases
            ],
        }
        snapshot_bytes = json.dumps(snapshot, sort_keys=True).encode()
        return LoadedDataset(
            name=dataset.name,
            version=dataset.version,
            source_path=dataset.source_path,
            sha256=hashlib.sha256(snapshot_bytes).hexdigest(),
            cases=redacted_cases,
            snapshot=snapshot,
        )

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
                _CORE_REDACTED_KEYS,
            )
        )
        return {secret for secret in secrets if secret}
