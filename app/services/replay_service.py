from typing import Any

from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.schemas.equipment_schema import EquipmentReplayMode, PackageType
from app.schemas.replay_schema import ReplayRunRequest
from app.schemas.stateful_schema import ReplayDiff
from app.services.adaptive_run_service import DeterministicGrayBoxRunService
from app.services.finding_fingerprint import finding_fingerprint
from app.services.run_service import DeterministicRunService
from app.services.stateful_run_service import StatefulRunService


class ReplayService:
    def __init__(
        self,
        repository: StatefulRepository,
        stateful_run_service: StatefulRunService,
        deterministic_run_service: DeterministicRunService,
        deterministic_graybox_service: DeterministicGrayBoxRunService,
        equipment_repository: EquipmentRepository | None = None,
        report_repository: RunRepository | None = None,
    ) -> None:
        self.repository = repository
        self.stateful_run_service = stateful_run_service
        self.deterministic_run_service = deterministic_run_service
        self.deterministic_graybox_service = deterministic_graybox_service
        self.equipment_repository = equipment_repository
        self.report_repository = report_repository

    async def replay(
        self,
        source_run_id: str,
        request: ReplayRunRequest,
    ) -> dict[str, Any]:
        source = await self.repository.get_run(source_run_id)
        snapshots = (
            await self.equipment_repository.list_snapshots(source_run_id)
            if self.equipment_repository is not None
            else []
        )
        if request.mode == EquipmentReplayMode.evidence_reevaluate:
            if request.evaluator_version != "core-evidence-v1":
                raise ValueError(f"evaluator version unavailable: {request.evaluator_version}")
            if self.report_repository is None:
                raise ValueError("Evidence re-evaluation repository is unavailable")
            rows = await self.report_repository.get_report_rows(source_run_id)
            event_ids = {event["id"] for event in rows["events"]}
            findings = [
                {
                    **finding,
                    "evidence_complete": bool(finding["evidence_event_ids"])
                    and set(finding["evidence_event_ids"]).issubset(event_ids),
                    "evaluator_version": request.evaluator_version,
                }
                for finding in rows["findings"]
            ]
            return {
                "source_run_id": source_run_id,
                "mode": request.mode.value,
                "status": "completed",
                "external_calls": 0,
                "findings": findings,
                "evidence_complete_count": sum(
                    finding["evidence_complete"] for finding in findings
                ),
                "equipment_snapshots": snapshots,
            }
        if request.mode == EquipmentReplayMode.same_binding_rerun:
            await self._require_historical_equipment(snapshots)
        stage = self._stage(source.mode)
        if stage == "stateful":
            if request.profile is None:
                raise ValueError("stateful replay requires profile")
            dataset = await self.repository.load_dataset(source_run_id)
            replay_result = await self.stateful_run_service.run_dataset(
                dataset=dataset,
                profile=request.profile,
                target_name=f"stateful-replay:{request.profile.value}",
                mode="replay_stateful",
            )
        elif stage == "blackbox":
            if request.target is None:
                raise ValueError("HTTP replay requires target credentials")
            dataset, budget = await self.repository.load_blackbox_replay_inputs(source_run_id)
            replay_result = await self.deterministic_run_service.run_dataset(
                target=request.target,
                dataset=dataset,
                budget=budget,
                mode="replay_blackbox",
            )
        elif stage == "graybox":
            if request.target is None:
                raise ValueError("HTTP replay requires target credentials")
            dataset, policy = await self.repository.load_graybox_replay_inputs(source_run_id)
            replay_result = await self.deterministic_graybox_service.run_dataset(
                target=request.target,
                dataset=dataset,
                policy=policy,
                mode="replay_graybox",
            )
        else:
            raise ValueError(f"unsupported replay source mode: {source.mode}")
        replay_run_id = str(replay_result["run_id"])
        diff = await self._compare(source_run_id, replay_run_id, stage=stage)
        replay = await self.repository.create_replay(
            source_run_id=source_run_id,
            replay_run_id=replay_run_id,
            diff=diff,
        )
        equipment_changes = self._equipment_changes(snapshots, request.equipment_bindings)
        return {
            **replay_result,
            "source_run_id": source_run_id,
            "mode": request.mode.value,
            "equipment_changes": equipment_changes,
            "replay": replay,
        }

    async def _require_historical_equipment(self, snapshots: list[dict[str, Any]]) -> None:
        if self.equipment_repository is None:
            return
        for snapshot in snapshots:
            package_type = PackageType(snapshot["package_type"])
            try:
                package = await self.equipment_repository.get_package(
                    package_type,
                    snapshot["package_id"],
                    snapshot["version"],
                )
            except LookupError as exc:
                raise ValueError(
                    f"historical equipment missing: {snapshot['package_id']} {snapshot['version']}"
                ) from exc
            if package["checksum"] != snapshot["checksum"]:
                raise ValueError(
                    f"historical equipment checksum unavailable: "
                    f"{snapshot['package_id']} {snapshot['version']}"
                )

    @staticmethod
    def _equipment_changes(
        snapshots: list[dict[str, Any]],
        bindings: dict[str, dict],
    ) -> dict[str, bool]:
        dimensions = {
            "target_changed": False,
            "provider_changed": False,
            "provider_instance_changed": False,
            "provider_config_changed": False,
            "skill_changed": False,
            "casepack_changed": False,
            "contract_changed": False,
            "policy_changed": False,
            "test_principal_changed": False,
        }
        for snapshot in snapshots:
            replacement = bindings.get(snapshot["package_id"])
            if replacement is None:
                continue
            package_type = snapshot["package_type"]
            changed = any(
                replacement.get(key) not in {None, snapshot.get(key)}
                for key in ("version", "checksum")
            )
            if package_type == "provider":
                dimensions["provider_changed"] |= changed
                dimensions["provider_instance_changed"] |= replacement.get(
                    "provider_instance_id"
                ) not in {None, snapshot.get("provider_instance_id")}
                dimensions["provider_config_changed"] |= replacement.get("config_revision") not in {
                    None,
                    snapshot.get("config_revision"),
                }
            elif package_type == "skill":
                dimensions["skill_changed"] |= changed
            elif package_type == "casepack":
                dimensions["casepack_changed"] |= changed
            elif package_type == "contract":
                dimensions["contract_changed"] |= changed
        return dimensions

    async def _compare(
        self,
        source_run_id: str,
        replay_run_id: str,
        *,
        stage: str,
    ) -> ReplayDiff:
        source_rows = await self.repository.finding_rows(source_run_id)
        replay_rows = await self.repository.finding_rows(replay_run_id)
        source = {self._fingerprint(row, stage): row for row in source_rows}
        replay = {self._fingerprint(row, stage): row for row in replay_rows}
        source_only = source.keys() - replay.keys()
        replay_only = replay.keys() - source.keys()
        shared = source.keys() & replay.keys()
        return ReplayDiff(
            fixed=sorted(str(source[key]["case_id"]) for key in source_only),
            new=sorted(
                str(replay[key]["case_id"]) for key in replay_only if not replay[key]["is_control"]
            ),
            persistent=sorted(str(source[key]["case_id"]) for key in shared),
            regressed=sorted(
                str(replay[key]["case_id"]) for key in replay_only if replay[key]["is_control"]
            ),
        )

    @staticmethod
    def _stage(mode: str) -> str:
        if "stateful" in mode:
            return "stateful"
        if "graybox" in mode:
            return "graybox"
        if mode in {"deterministic", "replay_blackbox"}:
            return "blackbox"
        return "unsupported"

    @staticmethod
    def _fingerprint(row: dict[str, Any], stage: str) -> str:
        stored = row.get("fingerprint")
        if stored:
            return str(stored)
        return finding_fingerprint(
            stage=stage,
            case_id=str(row["case_id"]),
            category=str(row["category"]),
            is_control=bool(row["is_control"]),
        )
