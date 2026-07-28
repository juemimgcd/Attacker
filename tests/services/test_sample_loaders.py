from app.schemas.attack_sample_schema import CaseKind
from app.services.sample_loader import (
    BlackBoxDatasetLoader,
    GrayBoxDatasetLoader,
    StatefulDatasetLoader,
)


async def test_all_v1_cases_load_with_attack_and_control_coverage() -> None:
    datasets = [
        await BlackBoxDatasetLoader().load("samples/blackbox/phase1.yaml"),
        await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml"),
        await StatefulDatasetLoader().load("samples/stateful/phase3.yaml"),
    ]

    assert [len(dataset.cases) for dataset in datasets] == [12, 10, 8]
    all_ids = [case.id for dataset in datasets for case in dataset.cases]
    assert len(all_ids) == len(set(all_ids)) == 30
    for dataset in datasets:
        kinds = {case.kind for case in dataset.cases}
        assert kinds == {CaseKind.attack, CaseKind.control}
