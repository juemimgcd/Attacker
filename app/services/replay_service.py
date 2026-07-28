from typing import Any

from app.repositories.stateful_repository import StatefulRepository
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
    ) -> None:
        self.repository = repository
        self.stateful_run_service = stateful_run_service
        self.deterministic_run_service = deterministic_run_service
        self.deterministic_graybox_service = deterministic_graybox_service

    async def replay(
        self,
        source_run_id: str,
        request: ReplayRunRequest,
    ) -> dict[str, Any]:
        source = await self.repository.get_run(source_run_id)
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
        return {**replay_result, "source_run_id": source_run_id, "replay": replay}

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
