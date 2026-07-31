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
    safe_role = SAFE_LOG_COMPONENT.sub(
        "-",
        role or os.environ.get("ATTACKER_PROCESS_ROLE", "app"),
    ).strip("-")
    safe_hostname = SAFE_LOG_COMPONENT.sub("-", hostname or socket.gethostname()).strip("-")
    return log_dir / (
        f"attacker-{safe_role or 'app'}-{safe_hostname or 'host'}-{pid or os.getpid()}.log"
    )


# 初始化 loguru 日志输出，统一配置控制台和文件日志。
def setup_logger() -> None:
    # 你要做的事：
    # 1. 创建日志目录
    # 2. 移除 loguru 默认 handler
    # 3. 添加控制台日志
    # 4. 添加文件日志
    # 5. 日志级别从 settings 读取
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
