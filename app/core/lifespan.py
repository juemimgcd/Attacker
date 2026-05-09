from contextlib import asynccontextmanager
from turtledemo.penrose import start

from fastapi import FastAPI
from loguru import logger

from conf.logging import setup_logger
from conf.settings import settings

# 创建 FastAPI 生命周期管理器，负责启动和关闭阶段的基础初始化。
def create_lifespan():
    # 你要做的事：
    # 1. 返回 FastAPI lifespan
    # 2. 启动时调用 setup_logger
    # 3. 启动时记录服务名和环境
    # 4. 关闭时记录 shutdown 日志
    # 5. Day 1 不强依赖真实外部服务
    # 管理单个 FastAPI 应用实例的启动日志初始化和关闭日志记录。
    @asynccontextmanager
    async def lifespan(_:FastAPI):
        setup_logger()
        logger.info(
            "attacker service start",
            service=settings.app.app_name,
            environment=settings.app.app_env,
        )
        yield
        logger.info(
            "attacker service stop",
            service=settings.app.app_name,
        )

    return lifespan



