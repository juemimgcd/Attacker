from prometheus_client import generate_latest

from app.observability import (
    EQUIPMENT_INVALID_PACKAGES,
    JOB_EXPIRED_LEASES,
    JOB_OLDEST_READY_AGE,
    JOB_STATE,
    METRICS_REFRESH_READY,
    READINESS,
    WORKER_STALE,
    record_equipment_invalid_packages,
    refresh_job_metrics,
)


def test_refresh_job_metrics_publishes_bounded_queue_and_worker_state() -> None:
    refresh_job_metrics(
        {
            "status_counts": {"queued": 3, "running": 2, "failed": 1},
            "oldest_ready_age_seconds": 47.5,
            "expired_leases": 2,
            "stale_workers": 1,
        }
    )

    payload = generate_latest().decode()
    assert 'attacker_jobs{status="queued"} 3.0' in payload
    assert 'attacker_jobs{status="running"} 2.0' in payload
    assert 'attacker_jobs{status="failed"} 1.0' in payload
    assert "attacker_job_oldest_ready_age_seconds 47.5" in payload
    assert "attacker_job_expired_leases 2.0" in payload
    assert "attacker_worker_stale 1.0" in payload
    assert "request_id" not in payload


def test_repository_gauges_use_latest_multiprocess_snapshot() -> None:
    gauges = (
        JOB_STATE,
        JOB_OLDEST_READY_AGE,
        JOB_EXPIRED_LEASES,
        WORKER_STALE,
        METRICS_REFRESH_READY,
    )
    assert {gauge._multiprocess_mode for gauge in gauges} == {"livemostrecent"}
    assert READINESS._multiprocess_mode == "livemostrecent"


def test_rule_bound_event_series_start_at_zero_and_invalid_count_is_a_gauge() -> None:
    record_equipment_invalid_packages(0)
    payload = generate_latest().decode()

    for name in (
        "provider_error",
        "cleanup_failure",
        "package_checksum_mismatch",
        "sandbox_termination",
    ):
        assert f'attacker_equipment_events_total{{name="{name}"}} 0.0' in payload
    for event in ("failed", "succeeded"):
        assert f'attacker_job_events_total{{event="{event}"}} 0.0' in payload
    assert "attacker_equipment_invalid_packages 0.0" in payload
    assert EQUIPMENT_INVALID_PACKAGES._multiprocess_mode == "livemostrecent"
