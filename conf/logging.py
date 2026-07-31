"""统一配置结构化控制台与滚动文件日志。"""

import os
import re
import socket
import sys
from pathlib import Path

from loguru import logger

from conf.settings import settings

SAFE_LOG_COMPONENT = re.compile(r"[^A-Za-z0-9._-]+")


def _process_log_path(
    log_dir: Path,
    *,
    role: str | None = None,
    hostname: str | None = None,
    pid: int | None = None,
) -> Path:
    """生成经过清洗且按角色、主机和进程隔离的日志文件路径。"""

    safe_role = SAFE_LOG_COMPONENT.sub(
        "-",
        role or os.environ.get("ATTACKER_PROCESS_ROLE", "app"),
    ).strip("-")
    safe_hostname = SAFE_LOG_COMPONENT.sub("-", hostname or socket.gethostname()).strip("-")
    return log_dir / (
        f"attacker-{safe_role or 'app'}-{safe_hostname or 'host'}-{pid or os.getpid()}.log"
    )


def setup_logger() -> None:
    """替换 Loguru 默认 handler，确保控制台和文件使用同一部署配置。"""

    log_dir = Path(settings.log.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    logger.remove()
    logger.add(
        sys.stderr,
        level=settings.log.log_level,
        enqueue=True,
        backtrace=settings.app.debug,
        diagnose=settings.app.debug,
        serialize=settings.log.structured_json,
    )
    logger.add(
        _process_log_path(log_dir),
        level=settings.log.log_level,
        rotation=settings.log.log_rotation,
        retention=settings.log.log_retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=settings.app.debug,
        diagnose=settings.app.debug,
        serialize=settings.log.structured_json,
    )
