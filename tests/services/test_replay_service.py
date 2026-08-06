from types import SimpleNamespace
from typing import Any, cast

import pytest

from app.schemas.replay_schema import ReplayRunRequest
from app.schemas.stateful_schema import StatefulProfile
from app.schemas.target_schema import TargetConfig
from app.services.replay_service import ReplayService


class FakeRepository:
    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.created: tuple[str, str] | None = None

    async def get_run(self, run_id: str):
        return SimpleNamespace(id=run_id, mode=self.mode)

    async def load_dataset(self, run_id: str):
        return f"stateful:{run_id}"

    async def load_blackbox_replay_inputs(self, run_id: str):
        return f"blackbox:{run_id}", "budget"

    async def load_graybox_replay_inputs(self, run_id: str):
        return f"graybox:{run_id}", "policy"

    async def finding_rows(self, run_id: str):
        return []

    async def create_replay(self, *, source_run_id: str, replay_run_id: str, diff):
        self.created = (source_run_id, replay_run_id)
        return {"source_run_id": source_run_id, "replay_run_id": replay_run_id, "diff": diff}


class FakeStatefulService:
    async def run_dataset(self, **kwargs):
        assert kwargs["dataset"].startswith("stateful:")
        return {"run_id": "stateful-replay"}


class FakeBlackBoxService:
    async def run_dataset(self, **kwargs):
        assert kwargs["dataset"].startswith("blackbox:")
        assert kwargs["budget"] == "budget"
        assert kwargs["mode"] == "replay_blackbox"
        return {"run_id": "blackbox-replay"}


class FakeGrayBoxService:
    def __init__(self) -> None:
        self.last_kwargs: dict[str, Any] | None = None

    async def run_dataset(self, **kwargs):
        self.last_kwargs = kwargs
        assert kwargs["dataset"].startswith("graybox:")
        assert kwargs["policy"] == "policy"
        assert kwargs["mode"] == "replay_graybox"
        return {"run_id": "graybox-replay"}


def _target() -> TargetConfig:
    return TargetConfig.model_validate(
        {"name": "sandbox", "endpoint": "http://localhost:8001/evaluate"}
    )


def _service(mode: str) -> ReplayService:
    return ReplayService(
        cast(Any, FakeRepository(mode)),
        cast(Any, FakeStatefulService()),
        cast(Any, FakeBlackBoxService()),
        cast(Any, FakeGrayBoxService()),
    )


async def test_replay_dispatches_stateful_run_with_explicit_profile() -> None:
    result = await _service("stateful").replay(
        "source",
        ReplayRunRequest(profile=StatefulProfile.hardened),
    )

    assert result["run_id"] == "stateful-replay"


@pytest.mark.parametrize("mode", ["deterministic", "adaptive_graybox", "deterministic_graybox"])
async def test_replay_dispatches_http_run_with_resupplied_target(mode: str) -> None:
    result = await _service(mode).replay(
        "source",
        ReplayRunRequest(target=_target()),
    )

    expected = "blackbox-replay" if mode == "deterministic" else "graybox-replay"
    assert result["run_id"] == expected


async def test_graybox_replay_forwards_explicit_approval_authorization() -> None:
    graybox = FakeGrayBoxService()
    service = ReplayService(
        cast(Any, FakeRepository("deterministic_graybox")),
        cast(Any, FakeStatefulService()),
        cast(Any, FakeBlackBoxService()),
        cast(Any, graybox),
    )

    await service.replay("source", ReplayRunRequest(target=_target()))

    assert graybox.last_kwargs is not None
    assert graybox.last_kwargs["preauthorize_approvals"] is False

    await service.replay(
        "source",
        ReplayRunRequest(target=_target(), preauthorize_approvals=True),
    )

    assert graybox.last_kwargs is not None
    assert graybox.last_kwargs["preauthorize_approvals"] is True


@pytest.mark.parametrize(
    ("mode", "payload", "message"),
    [
        ("stateful", ReplayRunRequest(), "profile"),
        ("deterministic", ReplayRunRequest(), "target"),
        ("unknown", ReplayRunRequest(target=_target()), "unsupported"),
    ],
)
async def test_replay_rejects_missing_inputs_and_unknown_modes(
    mode: str,
    payload: ReplayRunRequest,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        await _service(mode).replay("source", payload)


async def test_replay_classifies_historical_findings_without_stored_fingerprints() -> None:
    repository = FakeRepository("deterministic")

    async def finding_rows(run_id: str):
        shared = {
            "fingerprint": None,
            "case_id": "shared",
            "category": "injection",
            "is_control": False,
        }
        if run_id == "source":
            return [
                shared,
                {
                    "fingerprint": None,
                    "case_id": "fixed",
                    "category": "leakage",
                    "is_control": False,
                },
            ]
        return [
            shared,
            {
                "fingerprint": None,
                "case_id": "new",
                "category": "tool",
                "is_control": False,
            },
            {
                "fingerprint": None,
                "case_id": "regressed",
                "category": "control",
                "is_control": True,
            },
        ]

    repository.finding_rows = finding_rows  # type: ignore[method-assign]
    service = ReplayService(
        cast(Any, repository),
        cast(Any, FakeStatefulService()),
        cast(Any, FakeBlackBoxService()),
        cast(Any, FakeGrayBoxService()),
    )

    diff = await service._compare("source", "replay", stage="blackbox")

    assert diff.fixed == ["fixed"]
    assert diff.new == ["new"]
    assert diff.persistent == ["shared"]
    assert diff.regressed == ["regressed"]
