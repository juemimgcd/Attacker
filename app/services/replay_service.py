import json
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
from app.services.target_binding import canonical_target_binding


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
        source_facts = (
            await self.equipment_repository.get_run_binding_facts(source_run_id)
            if self.equipment_repository is not None
            else None
        )
        if (
            request.mode == EquipmentReplayMode.same_binding_rerun
            and request.target is not None
            and source_facts is not None
            and canonical_target_binding(request.target)
            != canonical_target_binding(source_facts.get("target_config"))
        ):
            raise ValueError("same-binding replay target differs from the source Run")
        equipment_source_run_id = (
            source_run_id if request.mode == EquipmentReplayMode.same_binding_rerun else None
        )
        if (
            request.mode == EquipmentReplayMode.upgrade_comparison
            and not request.equipment_bindings
        ):
            raise ValueError("upgrade comparison requires explicit equipment_bindings")
        equipment_overrides = (
            request.equipment_bindings
            if request.mode == EquipmentReplayMode.upgrade_comparison
            else None
        )
        stage = self._stage(source.mode)
        if stage == "stateful":
            if request.profile is None:
                raise ValueError("stateful replay requires profile")
            if (
                request.mode == EquipmentReplayMode.same_binding_rerun
                and source_facts is not None
                and source_facts["policy"] is not None
                and request.profile.value != source_facts["policy"].get("profile")
            ):
                raise ValueError("same-binding replay profile differs from the source Run")
            dataset = await self.repository.load_dataset(source_run_id)
            source_target_config: dict[str, Any] = {}
            if source_facts is not None:
                candidate_target_config = source_facts.get("target_config")
                if isinstance(candidate_target_config, dict):
                    source_target_config = candidate_target_config
            replay_result = await self.stateful_run_service.run_dataset(
                dataset=dataset,
                profile=request.profile,
                target_name=str(
                    source_target_config.get("name") or f"stateful-replay:{request.profile.value}"
                ),
                mode="replay_stateful",
                equipment_source_run_id=equipment_source_run_id,
                equipment_overrides=equipment_overrides,
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
                equipment_source_run_id=equipment_source_run_id,
                equipment_overrides=equipment_overrides,
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
                test_principal_refs=(
                    source_facts["test_principal_refs"]
                    if request.mode == EquipmentReplayMode.same_binding_rerun
                    and source_facts is not None
                    else None
                ),
                equipment_source_run_id=equipment_source_run_id,
                equipment_overrides=equipment_overrides,
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
        if self.equipment_repository is not None:
            replay_snapshots = await self.equipment_repository.list_snapshots(replay_run_id)
            replay_facts = await self.equipment_repository.get_run_binding_facts(replay_run_id)
            equipment_changes = self._equipment_changes(
                snapshots,
                replay_snapshots,
                source_facts or {},
                replay_facts,
            )
        else:
            equipment_changes = self._equipment_changes(snapshots, [], {}, {})
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
        source_snapshots: list[dict[str, Any]],
        replay_snapshots: list[dict[str, Any]],
        source_facts: dict[str, Any],
        replay_facts: dict[str, Any],
    ) -> dict[str, bool]:
        def normalized(value: Any) -> str:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)

        def packages(package_type: str, keys: tuple[str, ...]) -> list[tuple[Any, ...]]:
            return sorted(
                tuple(normalized(snapshot.get(key)) for key in keys)
                for snapshot in source_snapshots
                if snapshot["package_type"] == package_type
            )

        def replay_packages(package_type: str, keys: tuple[str, ...]) -> list[tuple[Any, ...]]:
            return sorted(
                tuple(normalized(snapshot.get(key)) for key in keys)
                for snapshot in replay_snapshots
                if snapshot["package_type"] == package_type
            )

        identity = ("package_id", "version", "checksum")
        provider_binding = (
            "package_id",
            "provider_instance_id",
        )
        provider_config = (
            "package_id",
            "provider_instance_id",
            "config_revision",
            "config",
            "secret_binding_revision",
        )
        source_target = canonical_target_binding(source_facts.get("target_config"))
        replay_target = canonical_target_binding(replay_facts.get("target_config"))
        source_policy = ReplayService._semantic_policy(source_facts.get("policy"))
        replay_policy = ReplayService._semantic_policy(replay_facts.get("policy"))
        return {
            "target_changed": (
                source_facts.get("target_endpoint") != replay_facts.get("target_endpoint")
                or source_target != replay_target
                or source_facts.get("equipment_target_refs")
                != replay_facts.get("equipment_target_refs")
            ),
            "provider_changed": (
                packages("provider", identity) != replay_packages("provider", identity)
            ),
            "provider_instance_changed": (
                packages("provider", provider_binding)
                != replay_packages("provider", provider_binding)
            ),
            "provider_config_changed": (
                packages("provider", provider_config)
                != replay_packages("provider", provider_config)
            ),
            "skill_changed": (packages("skill", identity) != replay_packages("skill", identity)),
            "casepack_changed": (
                packages("casepack", identity) != replay_packages("casepack", identity)
            ),
            "contract_changed": (
                packages("contract", identity) != replay_packages("contract", identity)
            ),
            "policy_changed": source_policy != replay_policy,
            "test_principal_changed": (
                source_facts.get("test_principal_refs") != replay_facts.get("test_principal_refs")
            ),
        }

    @staticmethod
    def _semantic_policy(policy: Any) -> Any:
        if not isinstance(policy, dict):
            return policy
        normalized = dict(policy)
        normalized.pop("allowed_target_ids", None)
        return normalized

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
