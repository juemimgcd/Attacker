from contextlib import asynccontextmanager

from fastapi import FastAPI
from loguru import logger

from app.infrastructure.database import Database
from app.repositories.run_repository import RunRepository
from app.services.report_service import ReportService
from app.services.run_service import DeterministicRunService
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
    async def lifespan(app: FastAPI):
        setup_logger()
        database = Database(settings.database.url, echo=settings.database.echo)
        await database.initialize()
        repository = RunRepository(database.session_factory)
        app.state.database = database
        app.state.run_repository = repository
        app.state.run_service = DeterministicRunService(repository)
        app.state.report_service = ReportService(repository)
        logger.info(
            "attacker service start",
            service=settings.app.app_name,
            environment=settings.app.app_env,
        )
        try:
            yield
        finally:
            await database.dispose()
            logger.info(
                "attacker service stop",
                service=settings.app.app_name,
            )

    return lifespan
