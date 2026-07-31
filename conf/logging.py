"""统一配置结构化控制台与滚动文件日志。"""

import sys
from pathlib import Path

from loguru import logger

from conf.settings import settings


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
        log_dir / "attacker.log",
        level=settings.log.log_level,
        rotation=settings.log.log_rotation,
        retention=settings.log.log_retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=settings.app.debug,
        diagnose=settings.app.debug,
        serialize=settings.log.structured_json,
    )
