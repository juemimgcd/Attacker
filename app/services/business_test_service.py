"""有限并发业务测试引擎；每次尝试独立、无业务写入自动重试、逐步保留证据。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4
from xml.etree import ElementTree as ET

from app.equipment.security import redact
from app.infrastructure.model_provider import ModelProviderError
from app.schemas.business_test_schema import (
    BusinessScenario,
    BusinessSuite,
    CriterionVerdict,
    JudgeDecision,
    UserDecision,
)
from app.services.business_test_io import (
    BusinessExecutionError,
    BusinessTransport,
    credentials,
    json_pointer,
    render,
)
from app.services.business_test_model import PROMPT_VERSION, BusinessModelClient


def now() -> str:
    return datetime.now(UTC).isoformat()


class CallBudget:
    """单事件循环中同步领取预算；预留清理调用，取消时仍可释放已声明的数据。"""

    def __init__(self, maximum: int) -> None:
        self.maximum = maximum
        self.used = 0
        self.reserved = 0
        self.by_kind: Counter[str] = Counter()

    def reserve_cleanup(self) -> None:
        if self.used + self.reserved >= self.maximum:
            raise BusinessExecutionError("call_budget_exhausted")
        self.reserved += 1

    def take(self, kind: str) -> None:
        if kind == "cleanup":
            self.reserved -= 1
        elif self.used + self.reserved >= self.maximum:
            raise BusinessExecutionError("call_budget_exhausted")
        self.used += 1
        self.by_kind[kind] += 1


class BusinessArtifacts:
    def __init__(self, path: Path, secrets: tuple[str, ...]) -> None:
        # Each run receives a new directory; no previous evidence can be overwritten.
        path.mkdir(parents=True, exist_ok=False)
        self.path = path
        self.secrets = secrets

    def write(self, name: str, value: Any) -> None:
        destination = self.path / name
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        temporary.write_text(
            json.dumps(redact(value, self.secrets), ensure_ascii=False, indent=2, allow_nan=False),
            encoding="utf-8",
        )
        temporary.replace(destination)

    def junit(self, report: dict[str, Any]) -> None:
        counts = report["counts"]
        root = ET.Element(
            "testsuite",
            {
                "name": report["name"],
                "tests": str(len(report["tasks"])),
                "failures": str(counts.get("failed", 0)),
                "errors": str(
                    sum(
                        count
                        for status, count in counts.items()
                        if status not in {"passed", "failed"}
                    )
                ),
            },
        )
        for task in report["tasks"]:
            case = ET.SubElement(
                root,
                "testcase",
                {
                    "classname": task["scenario_id"],
                    "name": task["task_id"],
                    "time": str(task.get("duration_seconds", 0)),
                },
            )
            if task["status"] != "passed":
                element = ET.SubElement(
                    case,
                    "failure" if task["status"] == "failed" else "error",
                    {
                        "type": task["status"],
                        "message": task.get("reason", task["status"]),
                    },
                )
                element.text = f"Evidence: {task['task_id']}.json"
        content = ET.tostring(root, encoding="unicode", xml_declaration=True)
        # XML 1.0 rejects control characters even inside escaped attributes.
        content = "".join(
            character
            for character in content
            if character in "\t\n\r"
            or 0x20 <= ord(character) <= 0xD7FF
            or 0xE000 <= ord(character) <= 0xFFFD
            or 0x10000 <= ord(character) <= 0x10FFFF
        )
        (self.path / "junit.xml").write_text(content, encoding="utf-8")


class BusinessTestService:
    def __init__(self, suite: BusinessSuite, output: Path) -> None:
        self.suite = suite
        secrets = credentials(suite)
        self.secret_values = tuple(secrets.values())
        self.transport = BusinessTransport(secrets)
        self.simulator = BusinessModelClient(suite.simulator, secrets)
        self.judge = BusinessModelClient(suite.judge, secrets)
        self.run_id = f"business-{uuid4().hex}"
        self.artifacts = BusinessArtifacts(output / self.run_id, self.secret_values)
        self.budget = CallBudget(suite.limits.max_calls)
        self.results: dict[int, dict[str, Any]] = {}
        self.active_records: dict[int, dict[str, Any]] = {}
        self.session_owners: dict[str, str] = {}
        self.next_index = 0
        self.started_at = now()

    async def run(self) -> dict[str, Any]:
        config = self.suite.model_dump(mode="json")
        checksum = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
        self.artifacts.write(
            "manifest.json",
            {
                "run_id": self.run_id,
                "started_at": self.started_at,
                "config_sha256": checksum,
                "prompt_version": PROMPT_VERSION,
                "config": config,
            },
        )
        stop_reason = "completed"
        try:
            async with asyncio.timeout(self.suite.limits.run_timeout_seconds):
                async with asyncio.TaskGroup() as group:
                    for worker in range(
                        min(self.suite.limits.concurrency, self.suite.limits.max_tasks)
                    ):
                        group.create_task(self._worker(), name=f"{self.run_id}-worker-{worker}")
            if self.next_index < self.suite.limits.max_tasks:
                stop_reason = "call_budget_exhausted"
        except TimeoutError:
            stop_reason = "run_timeout"
        except asyncio.CancelledError:
            stop_reason = "cancelled"
            raise
        except Exception:
            stop_reason = "runner_error"
            raise
        finally:
            for index in range(self.suite.limits.max_tasks):
                if index not in self.results:
                    record = self.active_records.get(index) or self._record(index)
                    record.update(status="cancelled", reason=stop_reason, finished_at=now())
                    self._save(index, record)
            report = {
                "run_id": self.run_id,
                "name": self.suite.name,
                "started_at": self.started_at,
                "finished_at": now(),
                "target_version": self.suite.target_version,
                "config_sha256": checksum,
                "stop_reason": stop_reason,
                "counts": dict(Counter(item["status"] for item in self.results.values())),
                "calls": {"total": self.budget.used, "by_kind": dict(self.budget.by_kind)},
                "tasks": [self.results[index] for index in sorted(self.results)],
            }
            report["passed"] = stop_reason == "completed" and all(
                task["status"] == "passed" for task in report["tasks"]
            )
            report["scenarios"] = {
                scenario.id: dict(
                    Counter(
                        task["status"]
                        for task in report["tasks"]
                        if task["scenario_id"] == scenario.id
                    )
                )
                for scenario in self.suite.scenarios
            }
            self.artifacts.write("report.json", report)
            self.artifacts.junit(report)
        return {
            "run_id": self.run_id,
            "passed": report["passed"],
            "counts": report["counts"],
            "stop_reason": stop_reason,
            "report": str(self.artifacts.path / "report.json"),
            "junit": str(self.artifacts.path / "junit.xml"),
        }

    async def _worker(self) -> None:
        while self.next_index < self.suite.limits.max_tasks:
            if self.budget.used + self.budget.reserved >= self.budget.maximum:
                return
            index = self.next_index
            self.next_index += 1
            await self._execute(index)

    def _record(self, index: int) -> dict[str, Any]:
        scenario = self.suite.scenarios[index % len(self.suite.scenarios)]
        task_id = f"task-{index + 1:05d}"
        return {
            "task_id": task_id,
            "scenario_id": scenario.id,
            "run_id": self.run_id,
            "session_id": f"{self.run_id}-{task_id}",
            "status": "running",
            "reason": "",
            "started_at": None,
            "transcript": [],
            "events": [],
            "criteria": [],
        }

    def _save(self, index: int, record: dict[str, Any]) -> None:
        self.artifacts.write(f"{record['task_id']}.json", record)
        if record["status"] != "running":
            self.results[index] = {
                key: record[key]
                for key in ("task_id", "scenario_id", "status", "reason", "duration_seconds")
                if key in record
            }

    async def _execute(self, index: int) -> None:
        scenario = self.suite.scenarios[index % len(self.suite.scenarios)]
        record = self._record(index)
        self.active_records[index] = record
        record["started_at"] = now()
        started = time.monotonic()
        bindings: dict[str, Any] = {
            "run_id": self.run_id,
            "task_id": record["task_id"],
            "scenario_id": scenario.id,
            "session_id": record["session_id"],
            "setup": None,
            "target": None,
            "messages": record["transcript"],
            "query": "",
        }
        reserved = False
        touched = False
        phase = "configuration"
        self._save(index, record)
        try:
            async with asyncio.timeout(self.suite.limits.task_timeout_seconds):
                bindings["facts"] = render(scenario.user_facts, bindings)
                task = render(scenario.task, bindings)
                if scenario.cleanup:
                    self.budget.reserve_cleanup()
                    reserved = True
                phase = "setup"
                if scenario.setup:
                    touched = True
                    bindings["setup"] = await self._endpoint(
                        index, record, "setup", scenario.setup, bindings
                    )
                if self.suite.target.session_setup_pointer is not None:
                    session = json_pointer(
                        bindings["setup"], self.suite.target.session_setup_pointer
                    )
                    if not isinstance(session, str) or not session:
                        raise BusinessExecutionError("invalid_setup_session_id")
                    owner = self.session_owners.setdefault(session, record["task_id"])
                    if owner != record["task_id"]:
                        raise BusinessExecutionError("session_reused_across_tasks")
                    bindings["session_id"] = session
                    record["server_session_id"] = session
                phase = "conversation"
                touched = True
                await self._conversation(index, record, task, bindings)
                phase = "verification"
                verification = None
                if scenario.verification:
                    verification = await self._endpoint(
                        index, record, "verification", scenario.verification, bindings
                    )
                phase = "evaluation"
                await self._evaluate(index, record, scenario, bindings, verification)
        except asyncio.CancelledError:
            record.update(status="cancelled", reason="run_cancelled")
            raise
        except TimeoutError:
            record.update(status="error", reason=f"{phase}:task_timeout")
        except (BusinessExecutionError, ModelProviderError) as exc:
            record.update(status="error", reason=f"{phase}:{exc}")
        except Exception as exc:  # noqa: BLE001 - isolate target/config failures per task
            record.update(status="error", reason=f"{phase}:{type(exc).__name__}")
        finally:
            if reserved:
                if touched and scenario.cleanup:
                    try:
                        await self._endpoint(index, record, "cleanup", scenario.cleanup, bindings)
                    except asyncio.CancelledError:
                        record.update(status="cancelled", reason="cleanup_interrupted")
                        self._save(index, record)
                        raise
                    except Exception as exc:  # noqa: BLE001 - retain cleanup failure in report
                        record["outcome_before_cleanup"] = record["status"]
                        record.update(status="error", reason=f"cleanup:{type(exc).__name__}")
                else:
                    self.budget.reserved -= 1
            record["finished_at"] = now()
            record["duration_seconds"] = round(time.monotonic() - started, 3)
            self._save(index, record)
            self.active_records.pop(index, None)

    async def _endpoint(
        self, index: int, record: dict[str, Any], kind: str, endpoint: Any, bindings: dict[str, Any]
    ) -> Any:
        self.budget.take(kind)
        event: dict[str, Any] = {"kind": kind, "status": "started", "at": now()}
        record["events"].append(event)
        self._save(index, record)
        try:

            def observe(value: dict[str, Any]) -> None:
                event["response"] = value
                self._save(index, record)

            result = await self.transport.call(endpoint, bindings, on_observation=observe)
            event.update(status="completed", response=result)
            return result
        except BaseException as exc:
            event.update(
                status="error",
                error=str(exc) if isinstance(exc, BusinessExecutionError) else type(exc).__name__,
            )
            if isinstance(exc, BusinessExecutionError) and exc.evidence is not None:
                event["response"] = exc.evidence
            raise
        finally:
            self._save(index, record)

    async def _model(
        self, index: int, record: dict[str, Any], role: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        self.budget.take(role)
        event: dict[str, Any] = {"kind": role, "status": "started", "at": now()}
        record["events"].append(event)
        self._save(index, record)
        client = self.simulator if role == "simulator" else self.judge
        try:
            output, metadata = await client.infer(
                role=role,
                operation_id=f"{self.run_id}:{record['task_id']}:{len(record['events'])}",
                payload=redact(payload, self.secret_values),
                max_bytes=self.suite.limits.max_context_bytes,
            )
            event.update(status="completed", input=payload, **metadata)
            return output
        except BaseException as exc:
            event.update(status="error", error=type(exc).__name__)
            raise
        finally:
            self._save(index, record)

    async def _conversation(
        self, index: int, record: dict[str, Any], task: str, bindings: dict[str, Any]
    ) -> None:
        query = task
        for turn in range(self.suite.limits.max_turns):
            bindings["query"] = query
            record["transcript"].append({"role": "user", "content": query})
            response = await self._endpoint(index, record, "target", self.suite.target, bindings)
            bindings["target"] = response
            submission = response
            if self.suite.target.completion:
                response = await self._endpoint(
                    index, record, "completion", self.suite.target.completion, bindings
                )
                bindings["target"] = response
            text = json_pointer(response, self.suite.target.response_text_pointer)
            if not isinstance(text, str) or not text.strip():
                raise BusinessExecutionError("missing_assistant_text")
            record["transcript"].append({"role": "assistant", "content": text})
            if self.suite.target.session_response_pointer is not None:
                session = json_pointer(submission, self.suite.target.session_response_pointer)
                if not isinstance(session, str) or not session:
                    raise BusinessExecutionError("invalid_session_id")
                if turn > 0 and session != bindings["session_id"]:
                    raise BusinessExecutionError("session_changed")
                owner = self.session_owners.setdefault(session, record["task_id"])
                if owner != record["task_id"]:
                    raise BusinessExecutionError("session_reused_across_tasks")
                bindings["session_id"] = session
                record["server_session_id"] = session
            # Endpoint completion is optional; otherwise the user simulator decides when to stop.
            if self.suite.target.done_pointer is not None:
                done = json_pointer(response, self.suite.target.done_pointer)
                if not isinstance(done, bool):
                    raise BusinessExecutionError("done_field_must_be_boolean")
                if done:
                    record["conversation_stop"] = "target_done"
                    return
            decision = UserDecision.model_validate(
                await self._model(
                    index,
                    record,
                    "simulator",
                    {
                        "task": task,
                        "user_facts": bindings["facts"],
                        "conversation": record["transcript"],
                        "remaining_turns": self.suite.limits.max_turns - turn - 1,
                        "output_schema": UserDecision.model_json_schema(),
                    },
                )
            )
            if decision.action != "reply":
                record["conversation_stop"] = decision.action
                return
            query = decision.message
        record["conversation_stop"] = "turn_limit"

    async def _evaluate(
        self,
        index: int,
        record: dict[str, Any],
        scenario: BusinessScenario,
        bindings: dict[str, Any],
        verification: Any,
    ) -> None:
        verdicts: dict[str, CriterionVerdict] = {}
        semantic: list[dict[str, Any]] = []
        sources = {
            "conversation": record["transcript"],
            "target": bindings["target"],
            "verification": verification,
        }
        references: dict[str, str] = {}
        for criterion in scenario.criteria:
            reference = f"{criterion.source}:{criterion.id}"
            try:
                if sources[criterion.source] is None:
                    raise KeyError(criterion.source)
                observed = json_pointer(sources[criterion.source], criterion.pointer)
            except (KeyError, IndexError, ValueError):
                verdicts[criterion.id] = CriterionVerdict(
                    id=criterion.id,
                    status="failed"
                    if criterion.evaluator == "exists" and sources[criterion.source] is not None
                    else "inconclusive",
                    reason="required evidence is missing",
                    evidence_refs=[reference] if sources[criterion.source] is not None else [],
                )
                continue
            expected = render(criterion.expected, bindings)
            if criterion.evaluator == "semantic":
                references[criterion.id] = reference
                semantic.append(
                    {
                        "id": criterion.id,
                        "description": criterion.description,
                        "source": criterion.source,
                        "evidence_ref": reference,
                        "evidence": observed,
                    }
                )
                continue
            if criterion.evaluator == "equals":
                passed = type(observed) is type(expected) and observed == expected
            elif criterion.evaluator == "contains":
                if not isinstance(observed, (str, list, dict)) or (
                    isinstance(observed, str) and not isinstance(expected, str)
                ):
                    verdicts[criterion.id] = CriterionVerdict(
                        id=criterion.id,
                        status="inconclusive",
                        reason="contains received incompatible evidence types",
                    )
                    continue
                try:
                    passed = expected in observed
                except TypeError:
                    verdicts[criterion.id] = CriterionVerdict(
                        id=criterion.id,
                        status="inconclusive",
                        reason="contains requires a hashable dictionary key",
                    )
                    continue
            else:
                passed = True
            verdicts[criterion.id] = CriterionVerdict(
                id=criterion.id,
                status="passed" if passed else "failed",
                reason=f"{criterion.evaluator} {'matched' if passed else 'did not match'}",
                evidence_refs=[reference],
            )
        record["criteria"] = [item.model_dump() for item in verdicts.values()]
        self._save(index, record)
        if semantic:
            decision = JudgeDecision.model_validate(
                await self._model(
                    index,
                    record,
                    "judge",
                    {
                        "criteria": semantic,
                        "output_schema": JudgeDecision.model_json_schema(),
                    },
                )
            )
            returned = [item.id for item in decision.criteria]
            if len(returned) != len(set(returned)) or set(returned) != set(references):
                raise BusinessExecutionError("judge_criterion_ids_mismatch")
            for item in decision.criteria:
                if any(reference != references[item.id] for reference in item.evidence_refs):
                    raise BusinessExecutionError("judge_invalid_evidence_reference")
                if item.status != "inconclusive" and not item.evidence_refs:
                    item = item.model_copy(
                        update={"status": "inconclusive", "reason": "judge did not cite evidence"}
                    )
                verdicts[item.id] = item
        record["criteria"] = [
            verdicts[criterion.id].model_dump() for criterion in scenario.criteria
        ]
        statuses = {item.status for item in verdicts.values()}
        if "failed" in statuses:
            record.update(status="failed", reason="acceptance_criteria_failed")
        elif record["conversation_stop"] == "turn_limit":
            record.update(status="failed", reason="conversation_turn_limit")
        elif "inconclusive" in statuses or record["conversation_stop"] == "blocked":
            record.update(status="inconclusive", reason="insufficient_evidence_or_user_facts")
        else:
            record.update(status="passed", reason="all_acceptance_criteria_passed")
