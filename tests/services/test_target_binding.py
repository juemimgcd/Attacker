from pydantic import HttpUrl

from app.schemas.target_schema import TargetConfig
from app.services.target_binding import canonical_target_binding


def test_legacy_target_snapshot_gets_the_default_refusal_contract() -> None:
    target = TargetConfig(name="sandbox", endpoint=HttpUrl("http://127.0.0.1:9000/chat"))
    legacy_snapshot = target.model_dump(mode="json")
    legacy_snapshot.pop("refusal_status_codes")

    assert canonical_target_binding(legacy_snapshot) == canonical_target_binding(target)
