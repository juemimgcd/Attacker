from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from loguru import logger

from app.equipment.catalog import EquipmentCatalog
from app.equipment.metrics import EquipmentMetrics
from app.equipment.runner import EquipmentRunner
from app.equipment.security import SecretBroker
from app.infrastructure.checkpoint import open_checkpointer
from app.infrastructure.database import Database
from app.infrastructure.secrets import build_secret_broker
from app.repositories.adaptive_repository import AdaptiveRepository
from app.repositories.equipment_repository import EquipmentRepository
from app.repositories.job_repository import JobRepository
from app.repositories.run_repository import RunRepository
from app.repositories.stateful_repository import StatefulRepository
from app.services.adaptive_run_service import (
    AdaptiveRunService,
    DeterministicGrayBoxRunService,
)
from app.services.equipment_service import EquipmentService
from app.services.harness_service import HarnessService
from app.services.job_service import JobDispatcher
from app.services.replay_service import ReplayService
from app.services.report_service import ReportService
from app.services.run_service import DeterministicRunService
from app.services.stateful_run_service import StatefulRunService
from conf.settings import Settings, settings


@dataclass(slots=True)
class AppRuntime:
    database: Database
    checkpointer: Any
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
    job_dispatcher: JobDispatcher
    catalog_ready: bool

    def install_on(self, app: Any) -> None:
        for field_name in self.__dataclass_fields__:
            setattr(app.state, field_name, getattr(self, field_name))


@asynccontextmanager
async def create_runtime(
    config: Settings = settings,
    *,
    secret_broker: SecretBroker | None = None,
    recover_cleanups: bool = True,
) -> AsyncIterator[AppRuntime]:
    database = Database.from_settings(config.database)
    resolved_secret_broker = secret_broker or build_secret_broker(config.secrets)
    await database.initialize()
    try:
        async with (
            database.advisory_lock("attacker:checkpoint_schema_setup"),
            open_checkpointer(config.checkpoint),
        ):
            pass
        async with open_checkpointer(config.checkpoint, setup_schema=False) as checkpointer:
            run_repository = RunRepository(database.session_factory)
            adaptive_repository = AdaptiveRepository(database.session_factory)
            stateful_repository = StatefulRepository(database.session_factory)
            equipment_repository = EquipmentRepository(database.session_factory)
            job_repository = JobRepository(database.session_factory)
            equipment_metrics = EquipmentMetrics()
            equipment_service = EquipmentService(
                equipment_repository,
                EquipmentCatalog(config.equipment),
                equipment_metrics,
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
                checkpointer=checkpointer,
                equipment_service=equipment_service,
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
            job_dispatcher = JobDispatcher(
                deterministic_run_service=run_service,
                adaptive_run_service=adaptive_run_service,
                deterministic_graybox_service=deterministic_graybox_service,
                stateful_run_service=stateful_run_service,
            )
            yield AppRuntime(
                database=database,
                checkpointer=checkpointer,
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
                job_dispatcher=job_dispatcher,
                catalog_ready=True,
            )
    finally:
        await database.dispose()
