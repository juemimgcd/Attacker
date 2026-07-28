from typing import Any

from app.repositories.stateful_repository import StatefulRepository
from app.schemas.stateful_schema import ReplayDiff, ReplayRunRequest
from app.services.stateful_run_service import StatefulRunService


class ReplayService:
    def __init__(
        self,
        repository: StatefulRepository,
        stateful_run_service: StatefulRunService,
    ) -> None:
        self.repository = repository
        self.stateful_run_service = stateful_run_service

    async def replay(
        self,
        source_run_id: str,
        request: ReplayRunRequest,
    ) -> dict[str, Any]:
        source = await self.repository.get_run(source_run_id)
        if "stateful" not in source.mode:
            raise ValueError("only stateful runs can be replayed in phase 3")
        dataset = await self.repository.load_dataset(source_run_id)
        replay_result = await self.stateful_run_service.run_dataset(
            dataset=dataset,
            profile=request.profile,
            target_name=f"stateful-replay:{request.profile.value}",
            mode="replay_stateful",
        )
        replay_run_id = str(replay_result["run_id"])
        diff = await self._compare(source_run_id, replay_run_id)
        replay = await self.repository.create_replay(
            source_run_id=source_run_id,
            replay_run_id=replay_run_id,
            diff=diff,
        )
        return {**replay_result, "source_run_id": source_run_id, "replay": replay}

    async def _compare(self, source_run_id: str, replay_run_id: str) -> ReplayDiff:
        source_rows = await self.repository.finding_rows(source_run_id)
        replay_rows = await self.repository.finding_rows(replay_run_id)
        source = {str(row["fingerprint"]): row for row in source_rows if row["fingerprint"]}
        replay = {str(row["fingerprint"]): row for row in replay_rows if row["fingerprint"]}
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
