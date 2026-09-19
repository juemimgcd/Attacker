"""Import upstream benchmark evidence without claiming Core executed its tools.

Upstream environments and judges remain authoritative for their own benchmark.
No public benchmark is silently converted into a prompt-only black-box Case.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections import Counter
from pathlib import Path
from typing import Any, Literal

from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.benchmark_schema import (
    BenchmarkCase,
    BenchmarkImportPolicy,
    BenchmarkManifest,
    BenchmarkObservation,
    LoadedBenchmarkDataset,
)
from app.schemas.stateful_schema import ReplayDiff
from app.services.tool_trace_adapter import ToolTraceAdapter

SOURCES = {
    "agentdojo": {
        "url": "https://github.com/ethz-spylab/agentdojo",
        "revision": "089ed468cf3ed0322acc66b0211f26d9d90dbf60",
        "version": "v1.2.2",
        "license": "LICENSE",
        "paths": ["src/agentdojo", "pyproject.toml", "LICENSE"],
    },
    "injecagent": {
        "url": "https://github.com/uiuc-kang-lab/InjecAgent",
        "revision": "f19c9f2c79a41046eb13c03c51a24c567a8ffa07",
        "version": "base",
        "license": "LICENCE",
        "paths": ["src", "data", "LICENCE"],
    },
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def read_json(path: Path) -> Any:
    if path.is_symlink() or path.stat().st_size > 100_000_000:
        raise ValueError("benchmark input must be a regular file of at most 100 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as stream:
        json.dump(value, stream, indent=2, ensure_ascii=False)
        stream.write("\n")


def source_metadata(benchmark: str, source: Path) -> dict[str, Any]:
    spec = SOURCES[benchmark]
    source = source.resolve(strict=True)
    revision = subprocess.check_output(
        ["git", "-C", str(source), "rev-parse", "HEAD"], text=True
    ).strip()
    if revision != spec["revision"]:
        raise ValueError(f"unsupported {benchmark} revision; expected {spec['revision']}")
    paths = spec["paths"]
    dirty = subprocess.check_output(
        ["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all", "--", *paths],
        text=True,
    )
    if dirty.strip():
        raise ValueError("benchmark source/data must match the pinned clean checkout")
    files = (
        subprocess.check_output(["git", "-C", str(source), "ls-files", "-z", "--", *paths])
        .decode()
        .split("\0")
    )
    hashes = {}
    for name in filter(None, files):
        path = source / name
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("symlink in benchmark source")
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "benchmark": benchmark,
        "revision": revision,
        "source_url": spec["url"],
        "source_sha256": digest(hashes),
        "license": (source / str(spec["license"])).read_text(encoding="utf-8"),
    }


def injecagent_catalog(source: Path, version: str) -> dict[str, Any]:
    if version not in {"base", "enhanced"}:
        raise ValueError("InjecAgent setting must be base or enhanced")
    cases = []
    for suite in ("dh", "ds"):
        rows = read_json(source / "data" / f"test_cases_{suite}_{version}.json")
        for index, row in enumerate(rows):
            cases.append(
                BenchmarkCase(
                    id=f"injecagent:{version}:{suite}:{digest(row)[:24]}",
                    suite=suite,
                    category="direct_harm" if suite == "dh" else "data_stealing",
                    kind="attack",
                    user_task_id=str(index),
                    prompt=row["User Instruction"],
                    attack_goal=row["Expected Achievements"],
                    definition=row,
                ).model_dump(mode="json")
            )
    return {
        "cases": cases,
        "environment": {
            "tools": read_json(source / "data/tools.json"),
            "simulated_responses_sha256": hashlib.sha256(
                (source / "data/attacker_simulated_responses.json").read_bytes()
            ).hexdigest(),
            "evaluator": "src/output_parsing.py",
            "semantics": "simulated tool observations and predicted actions; not physical tool execution",
        },
    }


def select_cases(
    manifest: BenchmarkManifest, case_ids: list[str] | None, limit: int | None
) -> BenchmarkManifest:
    cases = manifest.cases
    if case_ids:
        unknown = set(case_ids) - {case.id for case in cases}
        if unknown:
            raise ValueError(f"unknown benchmark cases: {sorted(unknown)}")
        cases = [case for case in cases if case.id in case_ids]
    if limit is not None:
        if limit < 1:
            raise ValueError("limit must be positive")
        cases = cases[:limit]
    return manifest.model_copy(update={"cases": cases})


def load_observations(
    manifest: BenchmarkManifest, results: Path, target: str
) -> list[BenchmarkObservation]:
    """Read explicit result scope, rejecting mixed pipelines and duplicate artifacts."""
    found: dict[str, BenchmarkObservation] = {}
    known = {case.id: case for case in manifest.cases}
    if manifest.benchmark == "agentdojo":
        if manifest.protocol != {"attack": "important_instructions"}:
            raise ValueError("only the AgentDojo important_instructions protocol is supported")
        identities = {
            (case.suite, case.user_task_id, case.injection_task_id): case for case in manifest.cases
        }
        for path in sorted(results.rglob("*.json")):
            try:
                raw = read_json(path)
            except json.JSONDecodeError:
                parts = path.relative_to(results).parts
                file_identity = (
                    (parts[-4], parts[-3], None if path.stem == "none" else path.stem)
                    if len(parts) >= 4
                    else None
                )
                case = identities.get(file_identity) if file_identity else None
                if case is None:
                    raise ValueError(
                        "malformed artifact cannot be linked to an expected Case"
                    ) from None
                _add_observation(
                    found,
                    BenchmarkObservation(
                        case_id=case.id,
                        status="error",
                        reason="upstream artifact is incomplete JSON",
                        artifact=path.relative_to(results).as_posix(),
                        artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                        evidence={"parse_error": "incomplete_json"},
                    ),
                )
                continue
            if not isinstance(raw, dict) or "user_task_id" not in raw:
                continue
            if str(raw["user_task_id"]).startswith("injection_task_"):
                continue  # Upstream attack-feasibility checks are not user-task Cases.
            if not isinstance(raw.get("suite_name"), str):
                raise TypeError("AgentDojo suite_name must be a string")
            identity = (
                str(raw["suite_name"]),
                str(raw["user_task_id"]),
                raw.get("injection_task_id"),
            )
            case = identities.get(identity)
            if case is None:
                continue
            if raw.get("pipeline_name") != target:
                raise ValueError(
                    "results contain a different pipeline; select one result directory"
                )
            if raw.get("benchmark_version") != manifest.version:
                raise ValueError("AgentDojo result benchmark_version is missing or differs")
            expected_attack = manifest.protocol.get("attack") if case.kind == "attack" else None
            if raw.get("attack_type") != expected_attack:
                raise ValueError("AgentDojo attack differs from the frozen protocol")
            status: Literal["evaluated", "error", "not_evaluable"] = "evaluated"
            reason = "upstream task judge; for attack cases security=true means injection succeeded"
            if case.validation_error:
                status, reason = "not_evaluable", case.validation_error
            elif raw.get("error"):
                status, reason = "error", "upstream execution reported an error"
            elif not raw.get("messages") or any(
                type(raw.get(key)) is not bool for key in ("utility", "security")
            ):
                status, reason = "not_evaluable", "missing trace or strict boolean judge result"
            observation = BenchmarkObservation(
                case_id=case.id,
                status=status,
                attack_success=(
                    raw["security"] if status == "evaluated" and case.kind == "attack" else None
                ),
                utility=raw["utility"] if status == "evaluated" else None,
                reason=reason,
                artifact=path.relative_to(results).as_posix(),
                artifact_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                evidence=raw,
            )
            _add_observation(found, observation)
    else:
        if manifest.protocol != {"agent": "prompted", "prompt_type": "InjecAgent"}:
            raise ValueError("only the InjecAgent prompted protocol is supported")
        definitions = {
            (case.suite, tuple(sorted(case.definition)), digest(case.definition)): case
            for case in manifest.cases
        }
        for suite in ("dh", "ds"):
            shapes = {keys for group, keys, _ in definitions if group == suite}
            path = results / f"test_cases_{suite}_{manifest.version}.json"
            if not path.exists():
                continue
            if path.is_symlink() or path.stat().st_size > 100_000_000:
                raise ValueError("InjecAgent result exceeds input limits")
            content = path.read_text(encoding="utf-8")
            lines = content.splitlines()
            for line_number, line in enumerate(lines, 1):
                if not line.strip():
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    if line_number == len(lines) and not content.endswith("\n"):
                        break  # Torn final record has no trustworthy ID; its Case stays missing.
                    raise
                # Upstream copies the complete source Case into each JSONL result.
                matches = [
                    case
                    for keys in shapes
                    if all(key in raw for key in keys)
                    and (
                        case := definitions.get(
                            (suite, keys, digest({key: raw[key] for key in keys}))
                        )
                    )
                    is not None
                ]
                if not matches:
                    raise ValueError("InjecAgent result does not match the frozen source Cases")
                if len(matches) != 1:
                    raise ValueError("ambiguous InjecAgent source Case")
                case = matches[0]
                first, second = raw.get("eval"), raw.get("eval Step 2")
                needs_second = suite == "ds" and first == "succ"
                verdict = second if needs_second else first
                complete = (
                    verdict in {"succ", "unsucc"}
                    and bool(raw.get("output"))
                    and (not needs_second or bool(raw.get("output Step 2")))
                    and not raw.get("error")
                )
                observation = BenchmarkObservation(
                    case_id=case.id,
                    status="error"
                    if raw.get("error")
                    else ("evaluated" if complete else "not_evaluable"),
                    attack_success=verdict == "succ" if complete else None,
                    reason="upstream predicted-action judge; data stealing requires both stages",
                    artifact=f"{path.name}:{line_number}",
                    artifact_sha256=hashlib.sha256(line.encode()).hexdigest(),
                    evidence=raw,
                )
                _add_observation(found, observation)
    return [
        found.get(case_id)
        or BenchmarkObservation(
            case_id=case_id,
            status="not_evaluable",
            reason=known[case_id].validation_error or "expected Case has no result artifact",
        )
        for case_id in known
    ]


def _add_observation(found: dict[str, BenchmarkObservation], item: BenchmarkObservation) -> None:
    if item.case_id in found:
        raise ValueError("duplicate Case result; separate attack variants/repetitions into runs")
    found[item.case_id] = item


def benchmark_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    def metric(key: str, kind: str) -> dict[str, Any]:
        selected = [row for row in results if row["case"]["kind"] == kind]
        values = [
            row["benchmark"][key]
            for row in selected
            if row["benchmark"]["status"] == "evaluated" and type(row["benchmark"].get(key)) is bool
        ]
        return {
            "numerator": sum(values),
            "denominator": len(values),
            "expected": len(selected),
            "rate": sum(values) / len(values) if values else None,
        }

    return {
        "provenance": "upstream_artifacts; not Core-observed physical Target calls",
        "statuses": dict(Counter(row["benchmark"]["status"] for row in results)),
        "attack_success_rate": metric("attack_success", "attack"),
        "clean_utility": metric("utility", "control"),
        "utility_under_attack": metric("utility", "attack"),
    }


class BenchmarkService:
    def __init__(self, repository: RunRepository) -> None:
        self.repository = repository

    async def ingest(
        self,
        manifest: BenchmarkManifest,
        observations: list[BenchmarkObservation],
        target: str,
        execution: dict[str, Any] | None = None,
    ) -> str:
        if not target.strip() or len(target) > 200:
            raise ValueError("target label must be nonempty and at most 200 characters")
        if [row.case_id for row in observations] != [case.id for case in manifest.cases]:
            raise ValueError("observations must match the frozen Case order")
        sanitizer = ToolTraceAdapter()
        snapshot = sanitizer.sanitize(
            manifest.model_dump(mode="json"), redacted_fields=set(), secret_values=set()
        )
        run_id = await self.repository.create_run(
            target_snapshot={
                "name": target,
                "endpoint": f"benchmark://{manifest.benchmark}",
                "provenance": "caller-labelled upstream artifacts",
            },
            dataset=LoadedBenchmarkDataset(
                name=manifest.benchmark,
                version=manifest.version,
                source_path=Path("upstream"),
                sha256=digest(snapshot),
                cases=manifest.cases,
                snapshot=snapshot,
            ),
            budget=BenchmarkImportPolicy(),
            mode="benchmark_import",
        )
        try:
            await self.repository.events.append(
                run_id=run_id,
                operation_id=f"{run_id}:benchmark_source",
                event_type="benchmark_source_frozen",
                evidence={
                    "benchmark": manifest.benchmark,
                    "revision": manifest.revision,
                    "version": manifest.version,
                    "source_sha256": manifest.source_sha256,
                    "protocol": manifest.protocol,
                    "execution": execution or {"origin": "import"},
                },
            )
            for index, (case, observation) in enumerate(zip(manifest.cases, observations), 1):
                await self.repository.record_benchmark_result(
                    run_id=run_id,
                    sequence=index,
                    case=sanitizer.sanitize(
                        case.model_dump(mode="json"), redacted_fields=set(), secret_values=set()
                    ),
                    observation=sanitizer.sanitize(
                        observation.model_dump(mode="json"),
                        redacted_fields=set(),
                        secret_values=set(),
                    ),
                )
            await self.repository.finalize_run(run_id, target_call_count=0)
        except BaseException:
            await self.repository.mark_run_interrupted(
                run_id,
                status="failed",
                target_call_count=0,
                reason_code="benchmark_import_interrupted",
                last_operation_id=f"{run_id}:import",
                partial_case_id=None,
                partial_calls=[],
            )
            raise
        return run_id

    async def compare(self, source_id: str, replay_id: str) -> dict[str, Any]:
        if source_id == replay_id:
            raise ValueError("comparison requires two different runs")
        source = await self.repository.get_report_rows(source_id)
        replay = await self.repository.get_report_rows(replay_id)
        if any(row["run"]["mode"] != "benchmark_import" for row in (source, replay)):
            raise ValueError("benchmark comparison requires benchmark runs")
        if source["dataset"]["sha256"] != replay["dataset"]["sha256"]:
            raise ValueError(
                "comparison requires identical frozen source, environment and Case set"
            )
        before = {row["case_id"]: row["result"] for row in source["steps"]}
        after = {row["case_id"]: row["result"] for row in replay["steps"]}
        diff = ReplayDiff()
        unresolved = []
        for case_id in sorted(before.keys() | after.keys()):
            left, right = before.get(case_id), after.get(case_id)
            if (
                not left
                or not right
                or any(row["benchmark"]["status"] != "evaluated" for row in (left, right))
            ):
                unresolved.append(case_id)
                continue
            control = left["case"]["kind"] == "control"
            old = (
                not left["benchmark"]["utility"] if control else left["benchmark"]["attack_success"]
            )
            new = (
                not right["benchmark"]["utility"]
                if control
                else right["benchmark"]["attack_success"]
            )
            if old and new:
                diff.persistent.append(case_id)
            elif old and not new:
                diff.fixed.append(case_id)
            elif new:
                (diff.regressed if control else diff.new).append(case_id)
        if unresolved:
            # No partial Replay record: existing reports must never label missing evidence fixed.
            return {
                "status": "not_comparable",
                "unresolved_case_ids": unresolved,
                "source_run_id": source_id,
                "replay_run_id": replay_id,
            }
        repository = StatefulRepository(self.repository.session_factory)
        for existing in await repository.list_replays(replay_id):
            if existing["replay_run_id"] == replay_id and existing["source_run_id"] != source_id:
                raise ValueError("this replay run is already linked to a different source run")
        return await repository.create_replay(
            source_run_id=source_id, replay_run_id=replay_id, diff=diff
        )
