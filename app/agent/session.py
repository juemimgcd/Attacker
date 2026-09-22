"""Session 只记录安全的运行状态；SQL 事件 ID 同时作为恢复位置的身份。"""

from dataclasses import dataclass

from pydantic import TypeAdapter

from app.agent.state import RunState
from app.repositories.adaptive_repository import AdaptiveRepository

_STATE = TypeAdapter(RunState)


@dataclass(frozen=True)
class Session:
    id: str
    state: RunState


async def save_session(
    repository: AdaptiveRepository,
    state: RunState,
    *,
    resumed_from: str | None = None,
) -> str:
    return await repository.save_agent_session(
        state=_STATE.dump_python(state, mode="json"),
        resumed_from=resumed_from,
    )


async def load_session(repository: AdaptiveRepository, run_id: str) -> Session:
    record = await repository.load_agent_session(run_id)
    if record is None:
        raise ValueError("run has no native Agent Session; legacy graph checkpoints cannot resume")
    event_id, payload = record
    if payload.get("version") != 1:
        raise ValueError("unsupported Agent Session version")
    state = _STATE.validate_python(payload.get("state"))
    if state["run_id"] != run_id:
        raise ValueError("Agent Session belongs to a different run")
    return Session(id=event_id, state=state)
