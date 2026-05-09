from pathlib import Path
import sys

from loguru import logger

from conf.settings import settings


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
        backtrace=True,
        diagnose=settings.app.debug
    )
    logger.add(
        log_dir / "attacker.log",
        level=settings.log.log_level,
        rotation=settings.log.log_rotation,
        retention=settings.log.log_retention,
        encoding="utf-8",
        enqueue=True,
        backtrace=True,
        diagnose=settings.app.debug
    )
