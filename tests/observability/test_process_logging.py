from pathlib import Path

from conf.logging import _process_log_path


def test_process_log_paths_are_collision_free_and_sanitized() -> None:
    log_dir = Path("/var/log/attacker")

    api = _process_log_path(
        log_dir,
        role="api",
        hostname="api/host",
        pid=10,
    )
    worker = _process_log_path(
        log_dir,
        role="worker",
        hostname="worker:host",
        pid=10,
    )

    assert api.name == "attacker-api-api-host-10.log"
    assert worker.name == "attacker-worker-worker-host-10.log"
    assert api != worker
