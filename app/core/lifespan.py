from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from loguru import logger

from app.equipment.catalog import EquipmentCatalog
from app.equipment.runner import EquipmentRunner
from app.infrastructure.database import Database
from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.services.adaptive_run_service import (
    AdaptiveRunService,
    DeterministicGrayBoxRunService,
)
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.replay_service import ReplayService
from app.services.report_service import ReportService
from app.services.run_service import DeterministicRunService
from app.services.stateful_run_service import StatefulRunService
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
        adaptive_repository = AdaptiveRepository(database.session_factory)
        stateful_repository = StatefulRepository(database.session_factory)
        equipment_repository = EquipmentRepository(database.session_factory)
        equipment_service = EquipmentService(
            equipment_repository,
            EquipmentCatalog(settings.equipment),
        )
        harness_service = HarnessService(
            equipment_repository,
            equipment_service,
            EquipmentRunner(settings.equipment),
            settings.equipment,
        )
        await equipment_service.reload()
        checkpoint_path = Path(settings.checkpoint.database_path)
        checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_path)) as checkpointer:
            await checkpointer.setup()
            app.state.database = database
            app.state.run_repository = repository
            app.state.adaptive_repository = adaptive_repository
            app.state.stateful_repository = stateful_repository
            app.state.equipment_repository = equipment_repository
            app.state.equipment_service = equipment_service
            app.state.harness_service = harness_service
            app.state.run_service = DeterministicRunService(repository)
            app.state.adaptive_run_service = AdaptiveRunService(
                repository=adaptive_repository,
                checkpointer=checkpointer,
            )
            app.state.deterministic_graybox_service = DeterministicGrayBoxRunService(
                adaptive_repository
            )
            app.state.stateful_run_service = StatefulRunService(stateful_repository)
            app.state.replay_service = ReplayService(
                stateful_repository,
                app.state.stateful_run_service,
                app.state.run_service,
                app.state.deterministic_graybox_service,
                equipment_repository,
                repository,
            )
            app.state.report_service = ReportService(repository, equipment_repository)
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
