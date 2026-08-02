from app.schemas.attack_sample_schema import CaseKind
from app.services.sample_loader import (
    BlackBoxDatasetLoader,
    GrayBoxDatasetLoader,
    StatefulDatasetLoader,
)


async def test_all_builtin_cases_load_with_attack_and_control_coverage() -> None:
    datasets = [
        await BlackBoxDatasetLoader().load("samples/blackbox/phase1.yaml"),
        await GrayBoxDatasetLoader().load("samples/graybox/phase2.yaml"),
        await StatefulDatasetLoader().load("samples/stateful/phase3.yaml"),
    ]

    assert [len(dataset.cases) for dataset in datasets] == [64, 10, 8]
    all_ids = [case.id for dataset in datasets for case in dataset.cases]
    assert len(all_ids) == len(set(all_ids)) == 82
    for dataset in datasets:
        kinds = {case.kind for case in dataset.cases}
        assert kinds == {CaseKind.attack, CaseKind.control}


async def test_blackbox_categories_have_controls_and_actionable_detection_rules() -> None:
    dataset = await BlackBoxDatasetLoader().load("samples/blackbox/phase1.yaml")
    categories = {case.category for case in dataset.cases}

    assert len(categories) >= 14
    for category in categories:
        category_cases = [case for case in dataset.cases if case.category == category]
        assert any(case.kind == CaseKind.control for case in category_cases), category
        for case in category_cases:
            evaluator = case.evaluator
            assert (
                evaluator.violation_patterns
                or evaluator.exact_match_patterns
                or evaluator.canary
                or evaluator.max_latency_ms
                or evaluator.max_response_bytes
            ), case.id


async def test_expanded_blackbox_attacks_have_traceable_techniques() -> None:
    dataset = await BlackBoxDatasetLoader().load("samples/blackbox/phase1.yaml")
    enriched = [case for case in dataset.cases if case.technique_ids]

    assert dataset.version == "1.2"
    assert len(enriched) == 30
    assert all(case.kind == CaseKind.attack for case in enriched)
    assert all(case.attack_variant for case in enriched)
    assert len({case.attack_variant for case in enriched}) == len(enriched)
    allowed_techniques = {
        "OWASP LLM01:2025",
        "NISTAML.014",
        "NISTAML.015",
        "NISTAML.018",
        "NISTAML.035",
        "NISTAML.038",
    }
    assert all(
        technique in allowed_techniques
        for case in enriched
        for technique in case.technique_ids
    )
    delivery_counts = {
        mode: sum(case.delivery_mode == mode for case in enriched)
        for mode in {
            "direct",
            "multi_turn",
            "embedded_untrusted_content",
            "target_fixture",
        }
    }
    assert delivery_counts == {
        "direct": 15,
        "multi_turn": 5,
        "embedded_untrusted_content": 5,
        "target_fixture": 5,
    }
    assert all(
        case.setup_requirements
        for case in enriched
        if case.delivery_mode == "target_fixture"
    )
    for case in enriched:
        if case.delivery_mode == "target_fixture":
            assert case.evaluator.canary
            prompt_text = "\n".join(case.prompts).casefold()
            assert case.evaluator.canary.casefold() not in prompt_text
