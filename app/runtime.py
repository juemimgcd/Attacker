"""组装数据库、Session、仓库、服务、装备和后台恢复任务。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.agent.hooks import Hooks
from app.equipment.catalog import EquipmentCatalog
from app.equipment.metrics import EquipmentMetrics
from app.equipment.runner import EquipmentRunner
from app.equipment.security import SecretBroker
from app.infrastructure.database import Database
from app.infrastructure.secrets import build_secret_broker
from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.concurrency_repository import ConcurrencyRepository
from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.event_store import EventStore
from app.repositories.job_repository import JobRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.repositories.subagent_repository import SubagentRepository
from app.services.adaptive_run_service import (
    AdaptiveRunService,
    DeterministicGrayBoxRunService,
)
from app.services.concurrency_limiter import SharedConcurrencyLimiter
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.job_service import JobApplicationService, JobDispatcher
from app.services.replay_service import ReplayService
from app.services.report_service import ReportService
from app.services.run_service import DeterministicRunService
from app.services.stateful_run_service import StatefulRunService
from app.services.subagent_service import SubagentService
from conf.settings import Settings, settings


@dataclass(slots=True)
class AppRuntime:
    """持有一次应用进程的已初始化组件，并安装到 FastAPI app.state。"""

    database: Database
    run_repository: RunRepository
    adaptive_repository: AdaptiveRepository
    stateful_repository: StatefulRepository
    equipment_repository: EquipmentRepository
    job_repository: JobRepository
    equipment_metrics: EquipmentMetrics
    equipment_service: EquipmentService
    harness_service: HarnessService
    run_service: DeterministicRunService
    adaptive_run_service: AdaptiveRunService
    deterministic_graybox_service: DeterministicGrayBoxRunService
    stateful_run_service: StatefulRunService
    replay_service: ReplayService
    report_service: ReportService
    subagent_service: SubagentService
    job_application_service: JobApplicationService
    job_dispatcher: JobDispatcher
    catalog_ready: bool

    def install_on(self, app: Any) -> None:
        for field_name in self.__dataclass_fields__:
            if field_name.endswith("_repository"):
                continue
            setattr(app.state, field_name, getattr(self, field_name))


@asynccontextmanager
async def create_runtime(
    config: Settings = settings,
    *,
    secret_broker: SecretBroker | None = None,
    recover_cleanups: bool = True,
    agent_hooks: Hooks | None = None,
) -> AsyncIterator[AppRuntime]:
    """按依赖顺序构建运行时，并在 finally 中关闭任务与数据库。"""

    database = Database.from_settings(config.database)
    resolved_secret_broker = secret_broker or build_secret_broker(config.secrets)
    await database.initialize()
    try:
        event_store = EventStore(database.session_factory)
        run_repository = RunRepository(database.session_factory, event_store)
        adaptive_repository = AdaptiveRepository(database.session_factory, event_store)
        stateful_repository = StatefulRepository(database.session_factory, event_store)
        equipment_repository = EquipmentRepository(database.session_factory, event_store)
        job_repository = JobRepository(database.session_factory)
        equipment_metrics = EquipmentMetrics()
        equipment_service = EquipmentService(
            equipment_repository,
            EquipmentCatalog(config.equipment),
            equipment_metrics,
            secret_broker=resolved_secret_broker,
        )
        harness_service = HarnessService(
            equipment_repository,
            equipment_service,
            EquipmentRunner(config.equipment, equipment_metrics),
            config.equipment,
            secret_broker=resolved_secret_broker,
            metrics=equipment_metrics,
        )
        async with database.advisory_lock("attacker:equipment_catalog_reload"):
            await equipment_service.reload()
        if recover_cleanups:
            async with database.advisory_lock("attacker:equipment_cleanup_recovery"):
                cleanup_recovery = await harness_service.recover_pending_cleanups()
                if cleanup_recovery:
                    logger.info(
                        "equipment cleanup recovery completed",
                        lease_count=len(cleanup_recovery),
                    )
        run_service = DeterministicRunService(
            run_repository,
            equipment_service=equipment_service,
        )
        adaptive_run_service = AdaptiveRunService(
            repository=adaptive_repository,
            equipment_service=equipment_service,
            secret_broker=resolved_secret_broker,
            hooks=agent_hooks,
        )
        deterministic_graybox_service = DeterministicGrayBoxRunService(
            adaptive_repository,
            equipment_service=equipment_service,
        )
        stateful_run_service = StatefulRunService(
            stateful_repository,
            equipment_service=equipment_service,
        )
        replay_service = ReplayService(
            stateful_repository,
            stateful_run_service,
            run_service,
            deterministic_graybox_service,
            equipment_repository,
            run_repository,
        )
        report_service = ReportService(run_repository, equipment_repository)
        subagent_service = SubagentService(
            SubagentRepository(database.session_factory),
            adaptive_run_service,
            report_service,
            SharedConcurrencyLimiter(
                ConcurrencyRepository(database.session_factory), config.orchestrator_concurrency
            ),
        )
        job_application_service = JobApplicationService(
            job_repository,
            default_max_attempts=config.worker.max_attempts,
        )
        job_dispatcher = JobDispatcher(
            deterministic_run_service=run_service,
            adaptive_run_service=adaptive_run_service,
            deterministic_graybox_service=deterministic_graybox_service,
            stateful_run_service=stateful_run_service,
        )
        yield AppRuntime(
            database=database,
            run_repository=run_repository,
            adaptive_repository=adaptive_repository,
            stateful_repository=stateful_repository,
            equipment_repository=equipment_repository,
            job_repository=job_repository,
            equipment_metrics=equipment_metrics,
            equipment_service=equipment_service,
            harness_service=harness_service,
            run_service=run_service,
            adaptive_run_service=adaptive_run_service,
            deterministic_graybox_service=deterministic_graybox_service,
            stateful_run_service=stateful_run_service,
            replay_service=replay_service,
            report_service=report_service,
            subagent_service=subagent_service,
            job_application_service=job_application_service,
            job_dispatcher=job_dispatcher,
            catalog_ready=True,
        )
    finally:
        await database.dispose()
