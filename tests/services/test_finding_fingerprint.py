from app.services.finding_fingerprint import finding_fingerprint


def test_finding_fingerprint_is_stable() -> None:
    first = finding_fingerprint(
        stage="blackbox",
        case_id="case-1",
        category="prompt_injection",
        is_control=False,
    )
    second = finding_fingerprint(
        stage="blackbox",
        case_id="case-1",
        category="prompt_injection",
        is_control=False,
    )

    assert first == second
    assert len(first) == 64


def test_finding_fingerprint_includes_stage() -> None:
    blackbox = finding_fingerprint(
        stage="blackbox",
        case_id="case-1",
        category="prompt_injection",
        is_control=False,
    )
    graybox = finding_fingerprint(
        stage="graybox",
        case_id="case-1",
        category="prompt_injection",
        is_control=False,
    )

    assert blackbox != graybox
